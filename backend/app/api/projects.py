"""Projects and their ingestion keys."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.deps import audit, current_user, require_role
from app.db import tenant_session
from app.ingest.keys import forget_key, new_key
from app.models.projects import IngestKey, Project
from app.security.sessions import Principal

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    retention_days: int = Field(default=30, ge=1, le=3650)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    retention_days: int | None = Field(default=None, ge=1, le=3650)


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[Literal["ingest", "prompts:read"]] = Field(default=["ingest"], min_length=1)


def project_out(p: Project) -> dict:
    return {"id": str(p.id), "name": p.name, "retention_days": p.retention_days, "created_at": p.created_at.isoformat()}


def key_out(k: IngestKey) -> dict:
    return {
        "id": str(k.id), "name": k.name, "prefix": k.prefix, "scopes": k.scopes, "created_at": k.created_at.isoformat(),
        "revoked": k.revoked_at is not None, "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
    }  # fmt: skip


async def get_project(db, project_id: UUID) -> Project:
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:  # also what another organisation's project looks like
        raise HTTPException(404, "Project not found.")
    return project


@router.get("")
async def list_projects(user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(select(Project).order_by(Project.name))).scalars().all()
    return {"projects": [project_out(p) for p in rows]}


@router.post("", status_code=201)
async def create_project(body: ProjectIn, request: Request, admin: Principal = Depends(require_role("admin"))) -> dict:
    try:
        async with tenant_session(admin.org_id) as db:
            project = Project(org_id=admin.org_id, name=body.name.strip(), retention_days=body.retention_days)
            db.add(project)
            await db.flush()
            await db.refresh(project)
            audit(db, admin, "project.created", str(project.id), {"name": project.name}, request)
            return project_out(project)
    except IntegrityError as exc:
        raise HTTPException(409, "A project with this name already exists.") from exc


@router.patch("/{project_id}")
async def update_project(
    project_id: UUID, body: ProjectPatch, request: Request, admin: Principal = Depends(require_role("admin"))
) -> dict:
    changes = body.model_dump(exclude_none=True)
    try:
        async with tenant_session(admin.org_id) as db:
            project = await get_project(db, project_id)
            for field, value in changes.items():
                setattr(project, field, value)
            audit(db, admin, "project.updated", str(project.id), changes, request)
            await db.flush()
            return project_out(project)
    except IntegrityError as exc:
        raise HTTPException(409, "A project with this name already exists.") from exc


@router.get("/{project_id}/keys")
async def list_keys(project_id: UUID, admin: Principal = Depends(require_role("admin"))) -> dict:
    async with tenant_session(admin.org_id) as db:
        await get_project(db, project_id)
        rows = (
            await db.execute(select(IngestKey).where(IngestKey.project_id == project_id).order_by(IngestKey.created_at.desc()))
        ).scalars().all()  # fmt: skip
    return {"keys": [key_out(k) for k in rows]}


@router.post("/{project_id}/keys", status_code=201)
async def create_key(
    project_id: UUID, body: KeyIn, request: Request, admin: Principal = Depends(require_role("admin"))
) -> dict:
    plain, prefix, key_hash = new_key()
    async with tenant_session(admin.org_id) as db:
        await get_project(db, project_id)
        key = IngestKey(
            org_id=admin.org_id, project_id=project_id, name=body.name.strip(), key_hash=key_hash, prefix=prefix,
            scopes=sorted(set(body.scopes)), created_by=admin.id,
        )  # fmt: skip
        db.add(key)
        await db.flush()
        await db.refresh(key)
        audit(
            db, admin, "key.created", str(key.id), {"name": key.name, "prefix": prefix, "scopes": key.scopes}, request
        )
        # The only time the key itself leaves the server.
        return key_out(key) | {"key": plain}


@router.delete("/{project_id}/keys/{key_id}", status_code=204)
async def revoke_key(
    project_id: UUID, key_id: UUID, request: Request, admin: Principal = Depends(require_role("admin"))
) -> None:
    async with tenant_session(admin.org_id) as db:
        key = (
            await db.execute(select(IngestKey).where(IngestKey.id == key_id, IngestKey.project_id == project_id))
        ).scalar_one_or_none()
        if key is None:
            raise HTTPException(404, "Key not found.")
        if key.revoked_at is None:
            key.revoked_at = func.now()
            audit(db, admin, "key.revoked", str(key.id), {"name": key.name, "prefix": key.prefix}, request)
        key_hash = key.key_hash
    await forget_key(key_hash)  # takes effect on the next request, not when the cache expires


# ---- model prices, used to estimate the cost of incoming spans


class PriceIn(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    input_per_mtok: float = Field(ge=0, le=10_000)
    output_per_mtok: float = Field(ge=0, le=10_000)


prices_router = APIRouter(prefix="/api/prices", tags=["projects"])


@prices_router.get("")
async def list_prices(user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        found = (
            await db.execute(
                text("SELECT model, input_per_mtok, output_per_mtok, updated_at FROM prices ORDER BY model")
            )
        ).all()
    return {"prices": [{"model": r.model, "input_per_mtok": float(r.input_per_mtok), "output_per_mtok": float(r.output_per_mtok),
                        "updated_at": r.updated_at.isoformat()} for r in found]}  # fmt: skip


@prices_router.put("")
async def set_price(body: PriceIn, request: Request, admin: Principal = Depends(require_role("admin"))) -> dict:
    """Applies to spans received from now on; costs already stored are not recalculated."""
    async with tenant_session(admin.org_id) as db:
        await db.execute(
            text("""INSERT INTO prices (org_id, model, input_per_mtok, output_per_mtok) VALUES (:o, :m, :i, :p)
                    ON CONFLICT (org_id, model) DO UPDATE SET input_per_mtok = excluded.input_per_mtok,
                    output_per_mtok = excluded.output_per_mtok, updated_at = now()"""),
            {"o": admin.org_id, "m": body.model.strip(), "i": body.input_per_mtok, "p": body.output_per_mtok},
        )
        audit(db, admin, "price.set", body.model, body.model_dump(), request)
    return body.model_dump()
