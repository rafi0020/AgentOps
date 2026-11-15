"""Monitor rules, the alerts they raise, and online evaluators."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.api.deps import audit, current_user, require_role
from app.api.investigations import announce_run, queue_run
from app.api.projects import get_project
from app.db import tenant_session
from app.evals.evaluators import DETERMINISTIC
from app.models.evals import Evaluator
from app.models.monitors import MonitorAlert, MonitorRule
from app.models.projects import Project
from app.monitors.rules import evaluate_rules
from app.security.ratelimit import rate_limit
from app.security.sessions import Principal

router = APIRouter(prefix="/api/monitors", tags=["monitors"])
Writer = Depends(require_role("engineer", "admin"))
METRIC_TEXT = {
    "error_rate": "error rate",
    "p95_latency_ms": "p95 latency (ms)",
    "cost_per_hour": "cost per hour (USD)",
    "score_avg": "average score",
}


class RuleFilter(BaseModel):
    model: str | None = Field(default=None, max_length=120)
    tool: str | None = Field(default=None, max_length=120)
    operation: str | None = Field(default=None, max_length=60)


class RuleIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=120)
    metric: Literal["error_rate", "p95_latency_ms", "cost_per_hour", "score_avg"]
    evaluator_id: UUID | None = None
    filter: RuleFilter = Field(default_factory=RuleFilter)
    comparison: Literal["<", ">"]
    threshold: float
    window_minutes: Literal[5, 15, 30, 60, 180, 360, 1440] = 15
    min_calls: int = Field(default=20, ge=1, le=100_000)

    @model_validator(mode="after")
    def evaluator_for_scores(self):
        if (self.metric == "score_avg") != (self.evaluator_id is not None):
            raise ValueError("An average-score monitor needs an evaluator; other metrics must not have one.")
        return self


class OnlineIn(BaseModel):
    project_id: UUID
    evaluator_id: UUID
    sample_rate: float = Field(gt=0, le=1)


def describe(rule: MonitorRule) -> str:
    where = ", ".join(f"{k} {v}" for k, v in (rule.filter or {}).items() if v)
    return f"{METRIC_TEXT[rule.metric]}{f' ({where})' if where else ''} {'above' if rule.comparison == '>' else 'below'} {rule.threshold:g} over {rule.window_minutes} min"


def rule_out(r: MonitorRule) -> dict:
    return {"id": str(r.id), "project_id": str(r.project_id), "name": r.name, "metric": r.metric, "evaluator_id": str(r.evaluator_id) if r.evaluator_id else None,
            "filter": r.filter, "comparison": r.comparison, "threshold": r.threshold, "window_minutes": r.window_minutes,
            "min_calls": r.min_calls, "status": r.status, "description": describe(r)}  # fmt: skip


@router.get("/rules")
async def list_rules(user: Principal = Depends(current_user), project: UUID | None = None) -> dict:
    stmt = select(MonitorRule).order_by(MonitorRule.created_at)
    if project:
        stmt = stmt.where(MonitorRule.project_id == project)
    async with tenant_session(user.org_id) as db:
        return {"rules": [rule_out(r) for r in (await db.execute(stmt)).scalars()]}


@router.post("/rules", status_code=201)
async def create_rule(body: RuleIn, request: Request, user: Principal = Writer) -> dict:
    try:
        async with tenant_session(user.org_id) as db:
            await get_project(db, body.project_id)
            if (
                body.evaluator_id
                and not (
                    await db.execute(
                        select(Evaluator.id).where(
                            Evaluator.id == body.evaluator_id, Evaluator.project_id == body.project_id
                        )
                    )
                ).first()
            ):
                raise HTTPException(404, "Evaluator not found in this project.")
            rule = MonitorRule(org_id=user.org_id, project_id=body.project_id, name=body.name.strip(), metric=body.metric, evaluator_id=body.evaluator_id,
                               filter=body.filter.model_dump(exclude_none=True), comparison=body.comparison, threshold=body.threshold,
                               window_minutes=body.window_minutes, min_calls=body.min_calls, created_by=user.id)  # fmt: skip
            db.add(rule)
            await db.flush()
            await db.refresh(rule)
            audit(db, user, "monitor.created", str(rule.id), {"name": rule.name, "rule": describe(rule)}, request)
            return rule_out(rule)
    except IntegrityError as exc:
        raise HTTPException(409, "A monitor with this name already exists in the project.") from exc


@router.patch("/rules/{rule_id}")
async def set_status(
    rule_id: UUID, status: Literal["active", "paused"], request: Request, user: Principal = Writer
) -> dict:
    async with tenant_session(user.org_id) as db:
        rule = (await db.execute(select(MonitorRule).where(MonitorRule.id == rule_id))).scalar_one_or_none()
        if rule is None:
            raise HTTPException(404, "Monitor not found.")
        rule.status = status
        audit(db, user, "monitor.updated", str(rule.id), {"status": status}, request)
        return rule_out(rule)


@router.get("/alerts")
async def list_alerts(
    user: Principal = Depends(current_user), open_only: bool = False, limit: int = Query(100, ge=1, le=500)
) -> dict:
    stmt = (
        select(MonitorAlert, MonitorRule, Project.name).join(MonitorRule, MonitorRule.id == MonitorAlert.rule_id)
        .join(Project, Project.id == MonitorRule.project_id).order_by(MonitorAlert.window_start.desc()).limit(limit)
    )  # fmt: skip
    if open_only:
        stmt = stmt.where(MonitorAlert.acknowledged_at.is_(None))
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(stmt)).all()
        unacknowledged = await db.scalar(
            select(func.count()).select_from(MonitorAlert).where(MonitorAlert.acknowledged_at.is_(None))
        )
    return {
        "unacknowledged": unacknowledged,
        "alerts": [
            {"id": str(a.id), "rule": rule_out(r), "project": name, "window_start": a.window_start.isoformat(), "window_end": a.window_end.isoformat(),
             "value": a.value, "calls": a.calls, "acknowledged": a.acknowledged_at is not None,
             "investigation_run_id": str(a.investigation_run_id) if a.investigation_run_id else None}
            for a, r, name in rows
        ],
    }  # fmt: skip


@router.post("/alerts/{alert_id}/ack")
async def acknowledge(alert_id: UUID, request: Request, user: Principal = Writer) -> dict:
    async with tenant_session(user.org_id) as db:
        alert = (await db.execute(select(MonitorAlert).where(MonitorAlert.id == alert_id))).scalar_one_or_none()
        if alert is None:
            raise HTTPException(404, "Alert not found.")
        if alert.acknowledged_at is None:
            alert.acknowledged_by, alert.acknowledged_at = user.id, func.now()
            audit(db, user, "monitor.alert_acknowledged", str(alert.id), request=request)
    return {"acknowledged": True}


@router.post("/alerts/{alert_id}/investigate", status_code=202)
async def investigate(alert_id: UUID, request: Request, user: Principal = Writer) -> dict:
    """Start the Investigator on this alert. Asking twice returns the investigation already started.

    The alert row is locked while the run is queued, so two clicks at once cannot both start one."""
    async with tenant_session(user.org_id) as db:
        row = (
            await db.execute(
                select(MonitorAlert, MonitorRule, Project.name).join(MonitorRule, MonitorRule.id == MonitorAlert.rule_id)
                .join(Project, Project.id == MonitorRule.project_id).where(MonitorAlert.id == alert_id)
                .with_for_update(of=MonitorAlert)
            )
        ).first()  # fmt: skip
        if row is None:
            raise HTTPException(404, "Alert not found.")
        alert, rule, project_name = row
        if alert.investigation_run_id:
            return {"id": str(alert.investigation_run_id)}
        question = (
            f"Monitor '{rule.name}' on project {project_name} fired: {describe(rule)}. Measured {alert.value:.4g} over {alert.calls} calls "
            f"between {alert.window_start:%Y-%m-%d %H:%M} and {alert.window_end:%H:%M} UTC. What caused it?"
        )
        run_id = await queue_run(db, user, question, request)
        alert.investigation_run_id = run_id
    await announce_run(run_id)
    return {"id": str(run_id)}


@router.post("/evaluate", dependencies=[Depends(rate_limit("monitors_evaluate", 6, 60))])
async def evaluate_now(user: Principal = Writer) -> dict:
    return {"raised": await evaluate_rules(user.org_id, datetime.now(UTC))}


@router.get("/online")
async def list_online(project: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, project)
        rows = (
            await db.execute(
                sa.text(
                    "SELECT e.id, e.name, e.kind, o.sample_rate FROM online_evaluators o "
                    "JOIN evaluators e ON e.id = o.evaluator_id WHERE o.project_id = :p ORDER BY e.name"
                ),
                {"p": project},
            )
        ).all()
    return {
        "online": [
            {"evaluator_id": str(r.id), "name": r.name, "kind": r.kind, "sample_rate": r.sample_rate} for r in rows
        ]
    }


@router.put("/online", status_code=200)
async def set_online(body: OnlineIn, request: Request, user: Principal = Writer) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, body.project_id)
        evaluator = (
            await db.execute(
                select(Evaluator).where(Evaluator.id == body.evaluator_id, Evaluator.project_id == body.project_id)
            )
        ).scalar_one_or_none()
        if evaluator is None:
            raise HTTPException(404, "Evaluator not found in this project.")
        if evaluator.kind not in DETERMINISTIC:
            raise HTTPException(
                422, "Only deterministic evaluators can score live traffic. Use a judge in experiments."
            )
        table = sa.table(
            "online_evaluators", *[sa.column(c) for c in ("org_id", "project_id", "evaluator_id", "sample_rate")]
        )
        await db.execute(
            insert(table).values(org_id=user.org_id, project_id=body.project_id, evaluator_id=body.evaluator_id, sample_rate=body.sample_rate)
            .on_conflict_do_update(index_elements=["project_id", "evaluator_id"], set_={"sample_rate": body.sample_rate})
        )  # fmt: skip
        audit(db, user, "online_evaluator.set", str(evaluator.id), {"sample_rate": body.sample_rate}, request)
    return {"evaluator_id": str(body.evaluator_id), "sample_rate": body.sample_rate}


@router.delete("/online/{project_id}/{evaluator_id}", status_code=204)
async def remove_online(project_id: UUID, evaluator_id: UUID, request: Request, user: Principal = Writer) -> None:
    """Stop scoring live traffic with this evaluator. Scores already stored are kept."""
    async with tenant_session(user.org_id) as db:
        await get_project(db, project_id)
        removed = (
            await db.execute(
                sa.text("DELETE FROM online_evaluators WHERE project_id = :p AND evaluator_id = :e RETURNING 1"),
                {"p": project_id, "e": evaluator_id},
            )
        ).first()
        if removed is None:
            raise HTTPException(404, "This evaluator is not scoring live traffic in this project.")
        audit(db, user, "online_evaluator.removed", str(evaluator_id), {}, request)
