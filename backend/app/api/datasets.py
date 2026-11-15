"""Datasets, evaluators and human labels."""

import csv
import io
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.deps import audit, current_user, require_role
from app.api.projects import get_project
from app.db import tenant_session
from app.evals.calibration import calibration
from app.evals.evaluators import KINDS, EvaluatorConfigError, validate_config
from app.evals.runner import output_hash
from app.models.evals import Dataset, DatasetItem, Evaluator, ExperimentResult
from app.security.sessions import Principal

router = APIRouter(prefix="/api", tags=["evaluation"])
MAX_ITEMS_PER_REQUEST = 1000
MAX_UPLOAD = 5 * 1024 * 1024
MAX_ROWS = 10_000
Writer = Depends(require_role("engineer", "admin"))


class DatasetIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class ItemIn(BaseModel):
    input: Any
    expected: Any | None = None
    metadata: dict = Field(default_factory=dict)


class EvaluatorIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=120)
    kind: str
    config: dict = Field(default_factory=dict)


class LabelIn(BaseModel):
    evaluator_id: UUID
    result_id: UUID
    score: int = Field(ge=1, le=5)


async def get_dataset(db, dataset_id: UUID) -> Dataset:
    dataset = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(404, "Dataset not found.")
    return dataset


def item_out(i: DatasetItem) -> dict:
    return {
        "id": str(i.id),
        "input": i.input,
        "expected": i.expected,
        "metadata": i.item_metadata,
        "source_trace_id": i.source_trace_id,
    }


@router.get("/datasets")
async def list_datasets(project: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, project)
        rows = (
            await db.execute(
                select(Dataset, func.count(DatasetItem.id)).outerjoin(DatasetItem, DatasetItem.dataset_id == Dataset.id)
                .where(Dataset.project_id == project).group_by(Dataset.id).order_by(Dataset.created_at.desc())
            )
        ).all()  # fmt: skip
    return {
        "datasets": [
            {
                "id": str(d.id),
                "name": d.name,
                "description": d.description,
                "items": n,
                "created_at": d.created_at.isoformat(),
            }
            for d, n in rows
        ]
    }


@router.post("/datasets", status_code=201)
async def create_dataset(body: DatasetIn, request: Request, user: Principal = Writer) -> dict:
    try:
        async with tenant_session(user.org_id) as db:
            await get_project(db, body.project_id)
            dataset = Dataset(
                org_id=user.org_id,
                project_id=body.project_id,
                name=body.name.strip(),
                description=body.description,
                created_by=user.id,
            )
            db.add(dataset)
            await db.flush()
            audit(db, user, "dataset.created", str(dataset.id), {"name": dataset.name}, request)
            return {"id": str(dataset.id), "name": dataset.name}
    except IntegrityError as exc:
        raise HTTPException(409, "A dataset with this name already exists in the project.") from exc


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: UUID, request: Request, user: Principal = Writer) -> None:
    async with tenant_session(user.org_id) as db:
        dataset = await get_dataset(db, dataset_id)
        audit(db, user, "dataset.deleted", str(dataset.id), {"name": dataset.name}, request)
        await db.execute(delete(Dataset).where(Dataset.id == dataset_id))


@router.get("/datasets/{dataset_id}/items")
async def list_items(
    dataset_id: UUID,
    user: Principal = Depends(current_user),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    async with tenant_session(user.org_id) as db:
        dataset = await get_dataset(db, dataset_id)
        total = await db.scalar(
            select(func.count()).select_from(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
        )
        rows = (
            await db.execute(select(DatasetItem).where(DatasetItem.dataset_id == dataset_id).order_by(DatasetItem.seq).limit(limit).offset(offset))
        ).scalars().all()  # fmt: skip
    return {
        "dataset": {"id": str(dataset.id), "name": dataset.name, "project_id": str(dataset.project_id)},
        "total": total,
        "items": [item_out(i) for i in rows],
    }


async def _add(
    db, user: Principal, dataset: Dataset, items: list[ItemIn], source: list[str | None] | None = None
) -> int:
    for n, item in enumerate(items):
        db.add(DatasetItem(org_id=user.org_id, dataset_id=dataset.id, input=item.input, expected=item.expected,
                           item_metadata=item.metadata, source_trace_id=source[n] if source else None))  # fmt: skip
    await db.flush()
    return len(items)


@router.post("/datasets/{dataset_id}/items", status_code=201)
async def add_items(dataset_id: UUID, body: list[ItemIn], user: Principal = Writer) -> dict:
    if not 1 <= len(body) <= MAX_ITEMS_PER_REQUEST:
        raise HTTPException(422, f"Send between 1 and {MAX_ITEMS_PER_REQUEST} items at a time.")
    async with tenant_session(user.org_id) as db:
        return {"added": await _add(db, user, await get_dataset(db, dataset_id), body)}


@router.post("/datasets/{dataset_id}/items/upload", status_code=201)
async def upload_items(dataset_id: UUID, file: UploadFile, user: Principal = Writer) -> dict:
    """A CSV with an `input` column and an optional `expected` column; other columns become metadata. Or a JSON list."""
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "The file is larger than 5 MB.")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "The file must be UTF-8 text.") from exc
    if (file.filename or "").lower().endswith(".json"):
        try:
            items = [ItemIn(**row) for row in json.loads(content)]
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "The JSON file must be a list of objects with an 'input' field.") from exc
    else:
        reader = csv.DictReader(io.StringIO(content))
        if "input" not in (reader.fieldnames or []):
            raise HTTPException(422, "The CSV needs a column named 'input'.")
        items = [
            ItemIn(
                input=row["input"],
                expected=row.get("expected") or None,
                metadata={k: v for k, v in row.items() if k not in ("input", "expected") and k},
            )
            for row in reader
        ]
    if not 1 <= len(items) <= MAX_ROWS:
        raise HTTPException(422, f"The file must hold between 1 and {MAX_ROWS:,} rows.")
    async with tenant_session(user.org_id) as db:
        return {"added": await _add(db, user, await get_dataset(db, dataset_id), items)}


class FromTraces(BaseModel):
    trace_ids: list[str] = Field(min_length=1, max_length=200)


@router.post("/datasets/{dataset_id}/items/from-traces", status_code=201)
async def items_from_traces(dataset_id: UUID, body: FromTraces, user: Principal = Writer) -> dict:
    """Turn real traffic into test cases: each trace's first captured input and last captured output."""
    async with tenant_session(user.org_id) as db:
        dataset = await get_dataset(db, dataset_id)
        rows = (
            await db.execute(
                text("""SELECT trace_id,
                               (array_agg(content -> 'input' ORDER BY start_time) FILTER (WHERE content ? 'input'))[1] AS input,
                               (array_agg(content -> 'output' ORDER BY start_time DESC) FILTER (WHERE content ? 'output'))[1] AS output
                        FROM spans WHERE project_id = :p AND trace_id = ANY(:ids) GROUP BY trace_id"""),
                {"p": dataset.project_id, "ids": body.trace_ids},
            )
        ).all()
        usable = [r for r in rows if r.input is not None]
        if not usable:
            raise HTTPException(
                422,
                "None of these traces carry content. The sending application has to opt in to sending prompts and outputs.",
            )
        added = await _add(
            db, user, dataset, [ItemIn(input=r.input, expected=r.output) for r in usable], [r.trace_id for r in usable]
        )
    return {"added": added, "skipped_without_content": len(body.trace_ids) - added}


# ---- evaluators and labels


def evaluator_out(e: Evaluator) -> dict:
    return {"id": str(e.id), "name": e.name, "kind": e.kind, "config": e.config, "created_at": e.created_at.isoformat()}


@router.get("/evaluators")
async def list_evaluators(project: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, project)
        rows = (
            (await db.execute(select(Evaluator).where(Evaluator.project_id == project).order_by(Evaluator.name)))
            .scalars()
            .all()
        )
        out = []
        for e in rows:
            item = evaluator_out(e)
            if e.kind == "judge":
                c = await calibration(db, e.id)
                item["calibration"] = {"status": c.status, "why": c.why, "n": c.n, "kappa": c.kappa}
            out.append(item)
    return {"evaluators": out, "kinds": list(KINDS)}


@router.post("/evaluators", status_code=201)
async def create_evaluator(body: EvaluatorIn, request: Request, user: Principal = Writer) -> dict:
    try:
        validate_config(body.kind, body.config)
    except EvaluatorConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        async with tenant_session(user.org_id) as db:
            await get_project(db, body.project_id)
            evaluator = Evaluator(
                org_id=user.org_id,
                project_id=body.project_id,
                name=body.name.strip(),
                kind=body.kind,
                config=body.config,
                created_by=user.id,
            )
            db.add(evaluator)
            await db.flush()
            await db.refresh(evaluator)
            audit(
                db,
                user,
                "evaluator.created",
                str(evaluator.id),
                {"name": evaluator.name, "kind": evaluator.kind},
                request,
            )
            return evaluator_out(evaluator)
    except IntegrityError as exc:
        raise HTTPException(409, "An evaluator with this name already exists in the project.") from exc


@router.get("/evaluators/{evaluator_id}/calibration")
async def get_calibration(evaluator_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        evaluator = (await db.execute(select(Evaluator).where(Evaluator.id == evaluator_id))).scalar_one_or_none()
        if evaluator is None or evaluator.kind != "judge":
            raise HTTPException(404, "Judge not found.")
        return (await calibration(db, evaluator_id)).as_dict()


@router.post("/labels", status_code=201)
async def add_label(body: LabelIn, user: Principal = Writer) -> dict:
    """A person's own grade, 1 to 5, for an output an experiment produced. Used to check a judge."""
    async with tenant_session(user.org_id) as db:
        result = (
            await db.execute(select(ExperimentResult).where(ExperimentResult.id == body.result_id))
        ).scalar_one_or_none()
        evaluator = (await db.execute(select(Evaluator).where(Evaluator.id == body.evaluator_id))).scalar_one_or_none()
        if result is None or evaluator is None:
            raise HTTPException(404, "Result or evaluator not found.")
        if result.output_hash is None:
            raise HTTPException(409, "This item produced no output to grade.")
        await db.execute(
            text("""INSERT INTO human_labels (org_id, evaluator_id, item_id, output_hash, score, labelled_by)
                    VALUES (:o, :e, :i, :h, :s, :u)
                    ON CONFLICT (evaluator_id, item_id, output_hash, labelled_by) DO UPDATE SET score = excluded.score, created_at = now()"""),
            {
                "o": user.org_id,
                "e": evaluator.id,
                "i": result.item_id,
                "h": result.output_hash or output_hash(result.output),
                "s": body.score,
                "u": user.id,
            },
        )
    return {"recorded": True}
