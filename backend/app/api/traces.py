"""Reading traces: the list, one trace with its spans, and sessions."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.api.deps import current_user
from app.api.projects import get_project
from app.db import tenant_session
from app.security.sessions import Principal

router = APIRouter(prefix="/api", tags=["traces"])
MAX_RANGE = timedelta(days=31)
MAX_SPANS = 2000


def time_range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Default: the last 24 hours. A range is at most 31 days, so no request can scan the whole history."""
    end = end or datetime.now(UTC) + timedelta(minutes=1)
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "Times must include a timezone, for example 2026-11-14T09:00:00Z.")
    if start >= end:
        raise HTTPException(422, "'from' must be before 'to'.")
    if end - start > MAX_RANGE:
        raise HTTPException(422, "Choose a range of at most 31 days.")
    return start, end


def trace_out(r) -> dict:
    return {
        "trace_id": r.trace_id, "root_name": r.root_name, "status": r.status, "start_time": r.start_time.isoformat(),
        "duration_ms": round(r.duration_ms, 1), "span_count": r.span_count, "error_count": r.error_count,
        "tokens": int(r.input_tokens + r.output_tokens), "cost_usd": float(r.cost_usd), "models": r.models, "tools": r.tools,
        "session_id": r.session_id, "service_name": r.service_name, "service_version": r.service_version,
    }  # fmt: skip


@router.get("/traces")
async def list_traces(
    project: UUID,
    user: Principal = Depends(current_user),
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    status: Literal["ok", "error"] | None = None,
    model: str | None = Query(None, max_length=120),
    tool: str | None = Query(None, max_length=120),
    service_version: str | None = Query(None, max_length=60),
    session: str | None = Query(None, max_length=200),
    min_duration_ms: float | None = Query(None, ge=0),
    q: str | None = Query(None, max_length=100, description="Part of the root span's name."),
    cursor: str | None = Query(None, max_length=120),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    start, end = time_range(start, end)
    where = ["project_id = :project", "start_time >= :start", "start_time < :end"]
    params: dict = {"project": project, "start": start, "end": end, "limit": limit + 1}
    for clause, name, value in [
        ("status = :status", "status", status), (":model = ANY(models)", "model", model), (":tool = ANY(tools)", "tool", tool),
        ("service_version = :sv", "sv", service_version), ("session_id = :session", "session", session),
        ("duration_ms >= :min_ms", "min_ms", min_duration_ms),
    ]:  # fmt: skip
        if value is not None:
            where.append(clause)
            params[name] = value
    if q and q.strip():
        where.append("root_name ILIKE :q")
        params["q"] = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if cursor:
        # Keyset pagination: continue strictly after the last row seen, so pages never overlap or skip
        # even while new traces arrive.
        try:
            at, after_id = cursor.split("|", 1)
            params |= {"c_time": datetime.fromisoformat(at), "c_id": after_id}
        except ValueError as exc:
            raise HTTPException(422, "Invalid cursor.") from exc
        where.append("(start_time, trace_id) < (:c_time, :c_id)")
    sql = f"SELECT * FROM traces WHERE {' AND '.join(where)} ORDER BY start_time DESC, trace_id DESC LIMIT :limit"  # noqa: S608
    async with tenant_session(user.org_id) as db:
        await get_project(db, project)
        found = (await db.execute(text(sql), params)).all()
    page = found[:limit]
    more = len(found) > limit
    return {
        "traces": [trace_out(r) for r in page],
        "next_cursor": f"{page[-1].start_time.isoformat()}|{page[-1].trace_id}" if more else None,
    }


@router.get("/traces/{project_id}/{trace_id}")
async def get_trace(project_id: UUID, trace_id: str, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, project_id)
        summary = (
            await db.execute(
                text("SELECT * FROM traces WHERE project_id = :p AND trace_id = :t"), {"p": project_id, "t": trace_id}
            )
        ).first()
        if summary is None:
            raise HTTPException(404, "Trace not found.")
        spans = (
            await db.execute(
                text(
                    "SELECT * FROM spans WHERE project_id = :p AND trace_id = :t ORDER BY start_time, span_id LIMIT :n"
                ),
                {"p": project_id, "t": trace_id, "n": MAX_SPANS + 1},
            )
        ).all()
    by_id = {s.span_id: s for s in spans}

    def depth(s, seen=0) -> int:
        parent = by_id.get(s.parent_span_id)
        return 0 if parent is None or seen > 50 else 1 + depth(parent, seen + 1)

    may_see_content = user.role in ("engineer", "admin")  # prompts and outputs can hold customer data
    return (
        trace_out(summary)
        | {
            "truncated": len(spans) > MAX_SPANS,
            "spans": [
                {
                    "span_id": s.span_id,
                    "parent_span_id": s.parent_span_id,
                    "name": s.name,
                    "status": s.status,
                    "status_message": s.status_message,
                    "depth": depth(s),
                    "offset_ms": round((s.start_time - summary.start_time).total_seconds() * 1000, 1),
                    "duration_ms": round(s.duration_ms, 1),
                    "operation": s.operation,
                    "model": s.model,
                    "tool": s.tool,
                    "provider": s.provider,
                    "prompt_name": s.prompt_name,
                    "prompt_version": s.prompt_version,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "cost_usd": float(s.cost_usd) if s.cost_usd is not None else None,
                    "attributes": s.attributes,
                    "content": s.content if may_see_content else None,
                    "has_content": s.content is not None,
                }  # fmt: skip
                for s in spans[:MAX_SPANS]
            ],
        }
    )


@router.get("/sessions")
async def list_sessions(
    project: UUID,
    user: Principal = Depends(current_user),
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    start, end = time_range(start, end)
    async with tenant_session(user.org_id) as db:
        await get_project(db, project)
        found = (
            await db.execute(
                text("""SELECT session_id, count(*) AS traces, min(start_time) AS first, max(end_time) AS last,
                               sum(cost_usd) AS cost, sum(error_count) AS errors, sum(input_tokens + output_tokens) AS tokens
                        FROM traces WHERE project_id = :p AND session_id IS NOT NULL AND start_time >= :s AND start_time < :e
                        GROUP BY session_id ORDER BY max(end_time) DESC LIMIT :n"""),
                {"p": project, "s": start, "e": end, "n": limit},
            )
        ).all()
    return {
        "sessions": [
            {"session_id": r.session_id, "traces": r.traces, "first": r.first.isoformat(), "last": r.last.isoformat(),
             "cost_usd": float(r.cost), "errors": int(r.errors), "tokens": int(r.tokens)}
            for r in found
        ]
    }  # fmt: skip


@router.get("/sessions/{project_id}/{session_id}")
async def get_session(project_id: UUID, session_id: str, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_project(db, project_id)
        found = (
            await db.execute(
                text("SELECT * FROM traces WHERE project_id = :p AND session_id = :s ORDER BY start_time LIMIT 500"),
                {"p": project_id, "s": session_id},
            )
        ).all()
    if not found:
        raise HTTPException(404, "Session not found.")
    return {"session_id": session_id, "traces": [trace_out(r) for r in found]}
