"""Prompt registry: versions, the live ("production") version, and release requests."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import audit, current_user, require_role
from app.api.projects import get_project
from app.db import tenant_session
from app.ingest.keys import KeyPrincipal, require_scope
from app.models.evals import Evaluator, Experiment, Prompt, PromptVersion, ReleaseRequest
from app.models.identity import User
from app.prompts.gate import check_gate
from app.prompts.registry import variables_in
from app.security.sessions import Principal

router = APIRouter(tags=["prompts"])
Writer = Depends(require_role("engineer", "admin"))
NAME = r"^[a-z0-9][a-z0-9._-]*$"


class ModelSettings(BaseModel):
    """What a prompt version may set for the model call. Anything else is refused rather than stored unread."""

    model_config = ConfigDict(extra="forbid")
    max_tokens: int | None = Field(default=None, ge=1, le=32_000)
    temperature: float | None = Field(default=None, ge=0, le=2)


class PromptIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=80, pattern=NAME)
    text: str = Field(min_length=1, max_length=20_000)
    model_settings: ModelSettings = Field(default_factory=ModelSettings)


class VersionIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    model_settings: ModelSettings = Field(default_factory=ModelSettings)
    note: str | None = Field(default=None, max_length=300)


class GateSettings(BaseModel):
    primary_evaluator_id: UUID | None = None
    min_score: float | None = Field(default=None, ge=0, le=1)
    max_drop: float = Field(default=0.02, ge=0, le=1)


class ReleaseIn(BaseModel):
    to_version: int = Field(ge=1)
    experiment_id: UUID | None = None


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


async def get_prompt(db, prompt_id: UUID) -> Prompt:
    prompt = (await db.execute(select(Prompt).where(Prompt.id == prompt_id))).scalar_one_or_none()
    if prompt is None:
        raise HTTPException(404, "Prompt not found.")
    return prompt


def version_out(v: PromptVersion) -> dict:
    return {"version": v.version, "text": v.text, "variables": v.variables, "model_settings": v.model_settings, "note": v.note,
            "created_at": v.created_at.isoformat()}  # fmt: skip


def release_out(r: ReleaseRequest, requested_by: str | None = None, prompt_name: str | None = None) -> dict:
    return {
        "id": str(r.id), "prompt_id": str(r.prompt_id), "prompt": prompt_name, "from_version": r.from_version, "to_version": r.to_version,
        "experiment_id": str(r.experiment_id) if r.experiment_id else None, "gate": r.gate, "status": r.status, "reason": r.reason,
        "requested_by": requested_by, "created_at": r.created_at.isoformat(), "decided_at": r.decided_at.isoformat() if r.decided_at else None,
    }  # fmt: skip


@router.get("/api/prompts")
async def list_prompts(project: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, project)
        rows = (
            await db.execute(
                select(Prompt, func.max(PromptVersion.version)).outerjoin(PromptVersion, PromptVersion.prompt_id == Prompt.id)
                .where(Prompt.project_id == project).group_by(Prompt.id).order_by(Prompt.name)
            )
        ).all()  # fmt: skip
    return {
        "prompts": [
            {"id": str(p.id), "name": p.name, "production_version": p.production_version, "latest_version": latest}
            for p, latest in rows
        ]
    }


@router.post("/api/prompts", status_code=201)
async def create_prompt(body: PromptIn, request: Request, user: Principal = Writer) -> dict:
    try:
        async with tenant_session(user.org_id) as db:
            await get_project(db, body.project_id)
            prompt = Prompt(org_id=user.org_id, project_id=body.project_id, name=body.name, created_by=user.id)
            db.add(prompt)
            await db.flush()
            db.add(PromptVersion(org_id=user.org_id, prompt_id=prompt.id, version=1, text=body.text, variables=variables_in(body.text),
                                 model_settings=body.model_settings.model_dump(exclude_none=True), created_by=user.id))  # fmt: skip
            audit(db, user, "prompt.created", str(prompt.id), {"name": prompt.name}, request)
            return {"id": str(prompt.id), "name": prompt.name, "version": 1}
    except IntegrityError as exc:
        raise HTTPException(409, "A prompt with this name already exists in the project.") from exc


@router.get("/api/prompts/id/{prompt_id}")
async def prompt_detail(prompt_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        prompt = await get_prompt(db, prompt_id)
        versions = (
            (
                await db.execute(
                    select(PromptVersion)
                    .where(PromptVersion.prompt_id == prompt_id)
                    .order_by(PromptVersion.version.desc())
                )
            )
            .scalars()
            .all()
        )
        releases = (
            await db.execute(
                select(ReleaseRequest, User.email).outerjoin(User, User.id == ReleaseRequest.requested_by)
                .where(ReleaseRequest.prompt_id == prompt_id).order_by(ReleaseRequest.created_at.desc()).limit(50)
            )
        ).all()  # fmt: skip
    return {
        "id": str(prompt.id), "name": prompt.name, "project_id": str(prompt.project_id), "production_version": prompt.production_version,
        "gate": {"primary_evaluator_id": str(prompt.primary_evaluator_id) if prompt.primary_evaluator_id else None,
                 "min_score": prompt.min_score, "max_drop": prompt.max_drop},
        "versions": [version_out(v) for v in versions],
        "releases": [release_out(r, email) for r, email in releases],
    }  # fmt: skip


@router.post("/api/prompts/id/{prompt_id}/versions", status_code=201)
async def add_version(prompt_id: UUID, body: VersionIn, request: Request, user: Principal = Writer) -> dict:
    """Versions are never edited: a change is a new version, so what ran in production can always be read back."""
    async with tenant_session(user.org_id) as db:
        prompt = await get_prompt(db, prompt_id)
        latest = await db.scalar(select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt_id))
        version = PromptVersion(org_id=user.org_id, prompt_id=prompt.id, version=(latest or 0) + 1, text=body.text, variables=variables_in(body.text),
                                model_settings=body.model_settings.model_dump(exclude_none=True), note=body.note, created_by=user.id)  # fmt: skip
        db.add(version)
        await db.flush()
        audit(
            db,
            user,
            "prompt.version_created",
            str(prompt.id),
            {"name": prompt.name, "version": version.version},
            request,
        )
        return {"version": version.version}


@router.patch("/api/prompts/id/{prompt_id}/gate")
async def set_gate(
    prompt_id: UUID, body: GateSettings, request: Request, admin: Principal = Depends(require_role("admin"))
) -> dict:
    async with tenant_session(admin.org_id) as db:
        prompt = await get_prompt(db, prompt_id)
        if body.primary_evaluator_id is not None:
            found = (
                await db.execute(
                    select(Evaluator.id).where(
                        Evaluator.id == body.primary_evaluator_id, Evaluator.project_id == prompt.project_id
                    )
                )
            ).first()
            if found is None:
                raise HTTPException(404, "Evaluator not found in this project.")
        prompt.primary_evaluator_id, prompt.min_score, prompt.max_drop = (
            body.primary_evaluator_id,
            body.min_score,
            body.max_drop,
        )
        audit(db, admin, "prompt.gate_changed", str(prompt.id), body.model_dump(mode="json"), request)
    return body.model_dump(mode="json")


@router.post("/api/prompts/id/{prompt_id}/releases", status_code=201)
async def request_release(prompt_id: UUID, body: ReleaseIn, request: Request, user: Principal = Writer) -> dict:
    async with tenant_session(user.org_id) as db:
        prompt = await get_prompt(db, prompt_id)
        exists = (
            await db.execute(
                select(PromptVersion.id).where(
                    PromptVersion.prompt_id == prompt_id, PromptVersion.version == body.to_version
                )
            )
        ).first()
        if exists is None:
            raise HTTPException(404, "That version does not exist.")
        if prompt.production_version == body.to_version:
            raise HTTPException(409, "That version is already live.")
        experiment = None
        if body.experiment_id:
            experiment = (
                await db.execute(
                    select(Experiment).where(
                        Experiment.id == body.experiment_id, Experiment.project_id == prompt.project_id
                    )
                )
            ).scalar_one_or_none()
            if experiment is None:
                raise HTTPException(404, "Experiment not found in this project.")
        gate = await check_gate(db, prompt, body.to_version, experiment)
        release = ReleaseRequest(
            org_id=user.org_id, prompt_id=prompt.id, from_version=prompt.production_version, to_version=body.to_version,
            experiment_id=body.experiment_id, gate=gate.as_dict(), status="pending" if gate.passed else "blocked", requested_by=user.id,
        )  # fmt: skip
        db.add(release)
        await db.flush()
        await db.refresh(release)
        audit(
            db,
            user,
            "release.requested",
            str(release.id),
            {"prompt": prompt.name, "to_version": body.to_version, "status": release.status},
            request,
        )
        return release_out(release, user.email, prompt.name)


@router.get("/api/releases")
async def list_releases(
    user: Principal = Depends(current_user), status: Literal["pending", "blocked", "approved", "rejected", "all"] = "pending",
    limit: int = Query(50, ge=1, le=200),
) -> dict:  # fmt: skip
    stmt = (
        select(ReleaseRequest, User.email, Prompt.name).join(Prompt, Prompt.id == ReleaseRequest.prompt_id)
        .outerjoin(User, User.id == ReleaseRequest.requested_by).order_by(ReleaseRequest.created_at.desc()).limit(limit)
    )  # fmt: skip
    if status != "all":
        stmt = stmt.where(ReleaseRequest.status == status)
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(stmt)).all()
    return {"releases": [release_out(r, email, name) for r, email, name in rows]}


@router.post("/api/releases/{release_id}/decide")
async def decide(
    release_id: UUID, body: Decision, request: Request, admin: Principal = Depends(require_role("admin"))
) -> dict:
    async with tenant_session(admin.org_id) as db:
        release = (
            await db.execute(select(ReleaseRequest).where(ReleaseRequest.id == release_id).with_for_update())
        ).scalar_one_or_none()
        if release is None:
            raise HTTPException(404, "Release request not found.")
        if release.status != "pending":
            raise HTTPException(409, f"This request is {release.status} and cannot be decided.")
        sole_admin = False
        if release.requested_by == admin.id:
            # Two people for a release. The exception is an organisation with one administrator, and it is recorded.
            admins = await db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active))
            if admins > 1:
                raise HTTPException(403, "Someone other than the person who requested a release must decide it.")
            sole_admin = True
        prompt = (await db.execute(select(Prompt).where(Prompt.id == release.prompt_id).with_for_update())).scalar_one()
        if body.decision == "approved":
            experiment = await db.get(Experiment, release.experiment_id) if release.experiment_id else None
            gate = await check_gate(
                db, prompt, release.to_version, experiment
            )  # checked again: things may have changed since the request
            release.gate = gate.as_dict()
            if not gate.passed:
                release.status = "blocked"
                audit(
                    db,
                    admin,
                    "release.blocked",
                    str(release.id),
                    {"prompt": prompt.name, "to_version": release.to_version},
                    request,
                )
                await db.flush()
                result = release_out(release, prompt_name=prompt.name)
                blocked = True
            else:
                blocked = False
                prompt.production_version = release.to_version
        else:
            blocked = False
        if not blocked:
            release.status, release.reason = body.decision, (body.reason or "").strip() or None
            release.decided_by, release.decided_at = admin.id, func.now()
            audit(db, admin, f"release.{body.decision}", str(release.id),
                  {"prompt": prompt.name, "from_version": release.from_version, "to_version": release.to_version, "sole_admin": sole_admin}, request)  # fmt: skip
            await db.flush()
            await db.refresh(release)
            result = release_out(release, prompt_name=prompt.name)
    if blocked:
        raise HTTPException(
            409, "The gate no longer passes, so this release was blocked. Open it to see which check failed."
        )
    return result


# ---- for applications, with an ingestion key that has the prompts:read scope


@router.get("/v1/prompts/{name}")
async def live_prompt(
    name: str, request: Request, response: Response, key: KeyPrincipal = Depends(require_scope("prompts:read"))
):
    """The live version of a prompt in the key's project. Applications call this instead of hard-coding prompt text."""
    async with tenant_session(key.org_id) as db:
        row = (
            await db.execute(
                select(Prompt, PromptVersion).join(PromptVersion, (PromptVersion.prompt_id == Prompt.id) & (PromptVersion.version == Prompt.production_version))
                .where(Prompt.project_id == key.project_id, Prompt.name == name)
            )
        ).first()  # fmt: skip
    if row is None:
        raise HTTPException(404, "No live version of a prompt with this name in this project.")
    prompt, version = row
    etag = f'"{prompt.id}-{version.version}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=30"
    return {
        "name": prompt.name,
        "version": version.version,
        "text": version.text,
        "variables": version.variables,
        "model_settings": version.model_settings,
    }
