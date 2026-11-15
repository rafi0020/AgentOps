"""Removes data older than each project's retention period.

Whole daily partitions are dropped once no project anywhere still needs that day: that is instant and leaves
nothing behind. Days that some project still needs are trimmed row by row for the projects that do not.
"""

import logging
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import text

from app.db import system_session, tenant_session
from app.models.projects import Project
from app.worker import scheduler

log = logging.getLogger("agentops.retention")
spans = sa.table("spans", sa.column("project_id"), sa.column("start_day"))
traces = sa.table("traces", sa.column("project_id"), sa.column("start_time"))
rollups = sa.table("rollups_5m", sa.column("project_id"), sa.column("bucket"))
scores = sa.table("score_rollups_5m", sa.column("project_id"), sa.column("bucket"))


async def apply_retention(today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    dropped = trimmed = 0
    async with system_session() as db:
        # The earliest day any project, in any organisation, still keeps. Days before it can go entirely.
        keep_from = await db.scalar(
            text(
                "SELECT coalesce(min(CAST(:today AS date) - retention_days), CAST(:today AS date) - 30) FROM projects"
            ),
            {"today": today},
        )
        days = (await db.execute(text("SELECT span_partition_days()"))).scalars().all()
        for day in days:
            if day < keep_from:
                dropped += bool(await db.scalar(text("SELECT drop_span_partition(:day)"), {"day": day}))
        org_ids = (await db.execute(text("SELECT id FROM orgs"))).scalars().all()
    for org_id in org_ids:
        async with tenant_session(org_id) as db:
            for project in (await db.execute(sa.select(Project))).scalars():
                cutoff = today - timedelta(days=project.retention_days)
                moment = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC)
                gone = await db.execute(
                    sa.delete(spans)
                    .where(spans.c.project_id == project.id, spans.c.start_day < cutoff)
                    .returning(spans.c.start_day)
                )
                trimmed += len(gone.all())
                await db.execute(
                    sa.delete(traces).where(traces.c.project_id == project.id, traces.c.start_time < moment)
                )
                await db.execute(
                    sa.delete(rollups).where(rollups.c.project_id == project.id, rollups.c.bucket < moment)
                )
                await db.execute(sa.delete(scores).where(scores.c.project_id == project.id, scores.c.bucket < moment))
    if dropped or trimmed:
        log.info("retention applied", extra={"partitions_dropped": dropped, "spans_trimmed": trimmed})
    return {"partitions_dropped": dropped, "spans_trimmed": trimmed}


@scheduler.every(3600, name="apply_retention")
async def _scheduled() -> None:
    await apply_retention()
