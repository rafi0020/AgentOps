"""Experiments: run a dataset through a target, score it, compare two runs."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import audit, current_user, require_role
from app.api.projects import get_project
from app.config import get_settings
from app.db import tenant_session
from app.evals.compare import compare
from app.evals.targets import public_target, seal_target, validate_target_url
from app.models.evals import Dataset, DatasetItem, Evaluator, Experiment, ExperimentResult, Prompt, PromptVersion
from app.security.sessions import Principal

router = APIRouter(prefix="/api/experiments", tags=["evaluation"])
Writer = Depends(require_role("engineer", "admin"))


class PromptTarget(BaseModel):
    kind: Literal["prompt"]
    prompt_id: UUID
    version: int


class HttpTarget(BaseModel):
    kind: Literal["http"]
    url: str = Field(max_length=500)
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body_template: dict | None = None
    output_path: str = Field(default="", max_length=120)


class ExperimentIn(BaseModel):
    project_id: UUID
    dataset_id: UUID
    name: str = Field(min_length=1, max_length=120)
    target: PromptTarget | HttpTarget = Field(discriminator="kind")
    evaluator_ids: list[UUID] = Field(min_length=1, max_length=10)


def out(e: Experiment) -> dict:
    target = public_target(e.target)
    return {
        "id": str(e.id), "name": e.name, "project_id": str(e.project_id), "dataset_id": str(e.dataset_id), "status": e.status,
        "target": target, "evaluator_ids": [str(i) for i in e.evaluator_ids], "totals": e.totals, "error": e.error,
        "items_total": e.items_total, "items_done": e.items_done, "cost_usd": float(e.cost_usd),
        "created_at": e.created_at.isoformat(), "finished_at": e.finished_at.isoformat() if e.finished_at else None,
    }  # fmt: skip


async def get_experiment(db, experiment_id: UUID) -> Experiment:
    experiment = (await db.execute(select(Experiment).where(Experiment.id == experiment_id))).scalar_one_or_none()
    if experiment is None:
        raise HTTPException(404, "Experiment not found.")
    return experiment


@router.post("", status_code=202)
async def create(body: ExperimentIn, request: Request, user: Principal = Writer) -> dict:
    target = body.target.model_dump(mode="json")
    if body.target.kind == "http":
        try:
            validate_target_url(body.target.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        target = seal_target(target)
    async with tenant_session(user.org_id) as db:
        await get_project(db, body.project_id)
        dataset = (
            await db.execute(
                select(Dataset).where(Dataset.id == body.dataset_id, Dataset.project_id == body.project_id)
            )
        ).scalar_one_or_none()
        if dataset is None:
            raise HTTPException(404, "Dataset not found in this project.")
        total = await db.scalar(
            select(func.count()).select_from(DatasetItem).where(DatasetItem.dataset_id == dataset.id)
        )
        limit = get_settings().experiment_max_items
        if not 1 <= total <= limit:
            raise HTTPException(422, f"The dataset has {total} items; an experiment needs between 1 and {limit:,}.")
        found = (
            (
                await db.execute(
                    select(Evaluator.id).where(
                        Evaluator.id.in_(body.evaluator_ids), Evaluator.project_id == body.project_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(found) != len(set(body.evaluator_ids)):
            raise HTTPException(404, "An evaluator was not found in this project.")
        if body.target.kind == "prompt":
            version = (
                await db.execute(
                    select(PromptVersion.id).join(Prompt, Prompt.id == PromptVersion.prompt_id)
                    .where(Prompt.id == body.target.prompt_id, Prompt.project_id == body.project_id, PromptVersion.version == body.target.version)
                )
            ).first()  # fmt: skip
            if version is None:
                raise HTTPException(404, "That prompt version was not found in this project.")
        experiment = Experiment(
            org_id=user.org_id, project_id=body.project_id, dataset_id=dataset.id, name=body.name.strip(), target=target,
            evaluator_ids=list(dict.fromkeys(body.evaluator_ids)), items_total=total, created_by=user.id,
        )  # fmt: skip
        db.add(experiment)
        await db.flush()
        audit(
            db,
            user,
            "experiment.created",
            str(experiment.id),
            {"name": experiment.name, "target": target["kind"]},
            request,
        )
        return {"id": str(experiment.id)}


@router.get("")
async def list_experiments(
    project: UUID, user: Principal = Depends(current_user), limit: int = Query(50, ge=1, le=200)
) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, project)
        rows = (
            (
                await db.execute(
                    select(Experiment)
                    .where(Experiment.project_id == project)
                    .order_by(Experiment.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {"experiments": [out(e) for e in rows]}


@router.get("/compare")
async def compare_two(a: UUID, b: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        first, second = await get_experiment(db, a), await get_experiment(db, b)
        if first.dataset_id != second.dataset_id:
            raise HTTPException(409, "These experiments ran on different datasets, so their items cannot be compared.")
        results = {}
        for experiment in (first, second):
            rows = (
                await db.execute(
                    select(ExperimentResult.item_id, ExperimentResult.scores).where(
                        ExperimentResult.experiment_id == experiment.id
                    )
                )
            ).all()
            results[experiment.id] = {str(r.item_id): r.scores for r in rows}
    c = compare(results[first.id], results[second.id], first.totals or {}, second.totals or {})
    return {"a": out(first), "b": out(second), "per_evaluator": c.per_evaluator, "regressions": c.regressions,
            "improvements": c.improvements, "items": c.items[:500]}  # fmt: skip


@router.get("/{experiment_id}")
async def detail(
    experiment_id: UUID,
    user: Principal = Depends(current_user),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    async with tenant_session(user.org_id) as db:
        experiment = await get_experiment(db, experiment_id)
        rows = (
            await db.execute(
                select(ExperimentResult, DatasetItem).join(DatasetItem, DatasetItem.id == ExperimentResult.item_id)
                .where(ExperimentResult.experiment_id == experiment_id).order_by(DatasetItem.seq).limit(limit).offset(offset)
            )
        ).all()  # fmt: skip
    return out(experiment) | {
        "results": [
            {"id": str(r.id), "item_id": str(r.item_id), "input": i.input, "expected": i.expected, "output": r.output, "error": r.error,
             "latency_ms": r.latency_ms, "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None, "scores": r.scores}
            for r, i in rows
        ]
    }  # fmt: skip


@router.post("/{experiment_id}/cancel")
async def cancel(experiment_id: UUID, request: Request, user: Principal = Writer) -> dict:
    async with tenant_session(user.org_id) as db:
        experiment = await get_experiment(db, experiment_id)
        if experiment.status == "queued":
            experiment.status, experiment.finished_at = "cancelled", func.now()
        elif experiment.status == "running":
            experiment.cancel_requested = True
        audit(db, user, "experiment.cancelled", str(experiment_id), request=request)
        return {"status": experiment.status}
