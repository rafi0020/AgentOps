"""Figures for charts, read from roll-ups."""

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import current_user
from app.api.projects import get_project
from app.api.traces import time_range
from app.db import tenant_session
from app.ingest.rollups import DIMENSIONS, query_rollups
from app.redis import get_redis
from app.security.sessions import Principal

router = APIRouter(prefix="/api/metrics", tags=["metrics"])
CACHE_SECONDS = 30


async def _cached(user: Principal, name: str, params: dict, compute):
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:24]
    key = f"metrics:{user.org_id}:{name}:{digest}"  # the organisation is part of the key
    redis = get_redis()
    if cached := await redis.get(key):
        return json.loads(cached)
    value = await compute()
    await redis.set(key, json.dumps(value), ex=CACHE_SECONDS)
    return value


@router.get("/overview")
async def overview(
    user: Principal = Depends(current_user),
    project: UUID | None = None,
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
) -> dict:
    """Totals for the range, the same for the range just before it, and model calls on their own."""
    start, end = time_range(start, end)
    before = start - (end - start)

    async def compute() -> dict:
        async with tenant_session(user.org_id) as db:
            if project:
                await get_project(db, project)
            now = (await query_rollups(db, project, start, end))[0]
            previous = (await query_rollups(db, project, before, start))[0]
            model_calls = (await query_rollups(db, project, start, end, filters={"operation": "chat"}))[0]
            by_project = await query_rollups(db, project, start, end, group_by=["project_id"])
        return {"from": start.isoformat(), "to": end.isoformat(), "current": now, "previous": previous,
                "model_calls": model_calls, "by_project": by_project}  # fmt: skip

    return await _cached(user, "overview", {"p": project, "s": start, "e": end}, compute)


@router.get("/series")
async def series(
    user: Principal = Depends(current_user),
    project: UUID | None = None,
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    group_by: str = Query("", max_length=120, description="Comma-separated: " + ", ".join(DIMENSIONS)),
    step: Literal["5m", "1h", "1d"] | None = None,
    model: str | None = Query(None, max_length=120),
    operation: str | None = Query(None, max_length=60),
    tool: str | None = Query(None, max_length=120),
) -> dict:
    start, end = time_range(start, end)
    groups = [g.strip() for g in group_by.split(",") if g.strip()]
    filters = {"model": model, "operation": operation, "tool": tool}

    async def compute() -> dict:
        async with tenant_session(user.org_id) as db:
            if project:
                await get_project(db, project)
            rows = await query_rollups(db, project, start, end, groups, step, filters)
        return {"from": start.isoformat(), "to": end.isoformat(), "step": step, "rows": rows}

    return await _cached(
        user, "series", {"p": project, "s": start, "e": end, "g": groups, "t": step, "f": filters}, compute
    )
