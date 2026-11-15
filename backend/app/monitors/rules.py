"""Monitors: a metric of one project, over a window, compared with a threshold.

Evaluated every five minutes, on the five-minute grid, from roll-ups, over the most recent complete window. Windows
are fixed intervals of the rule's length (15 minutes ends at :00, :15, :30, :45), so a rule over a long window is
checked several times but raises at most one alert for each window. A window with too few calls raises nothing: an
error rate of 100% over one call is noise, not an incident.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.db import system_session, tenant_session
from app.ingest.rollups import query_rollups
from app.models.monitors import MonitorAlert, MonitorRule
from app.worker import scheduler

log = logging.getLogger("agentops.monitors")


def last_complete_window(now: datetime, minutes: int) -> tuple[datetime, datetime]:
    """The latest finished window of fixed length. Windows do not slide, so each one is decided once."""
    seconds = minutes * 60
    end = datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, UTC)
    return end - timedelta(seconds=seconds), end


async def measure(db, rule: MonitorRule, start: datetime, end: datetime) -> tuple[float | None, int]:
    """(value, number of calls it is based on)."""
    if rule.metric == "score_avg":
        row = (
            await db.execute(
                text(
                    "SELECT sum(count), sum(sum) FROM score_rollups_5m WHERE project_id = :p AND evaluator_id = :e AND bucket >= :s AND bucket < :t"
                ),
                {"p": rule.project_id, "e": rule.evaluator_id, "s": start, "t": end},
            )
        ).one()
        count = int(row[0] or 0)
        return (row[1] / count if count else None), count
    figures = (await query_rollups(db, rule.project_id, start, end, filters=rule.filter or {}))[0]
    calls = figures["calls"]
    if rule.metric == "error_rate":
        return figures["error_rate"], calls
    if rule.metric == "p95_latency_ms":
        return figures["p95_ms"], calls
    return figures["cost_usd"] / ((end - start).total_seconds() / 3600), calls  # cost_per_hour


async def evaluate_rules(org_id: UUID, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    raised = 0
    async with tenant_session(org_id) as db:
        rules = (await db.execute(select(MonitorRule).where(MonitorRule.status == "active"))).scalars().all()
        for rule in rules:
            start, end = last_complete_window(now, rule.window_minutes)
            value, calls = await measure(db, rule, start, end)
            if value is None or calls < rule.min_calls:
                continue
            breached = value > rule.threshold if rule.comparison == ">" else value < rule.threshold
            if not breached:
                continue
            created = await db.execute(
                insert(MonitorAlert)
                .values(org_id=org_id, rule_id=rule.id, window_start=start, window_end=end, value=value, calls=calls)
                .on_conflict_do_nothing(index_elements=["rule_id", "window_start"])
                .returning(MonitorAlert.id)
            )
            raised += len(created.all())
    return raised


@scheduler.every(300, name="evaluate_monitors", on_boundary=True)
async def evaluate_all() -> None:
    async with system_session() as db:
        org_ids = (await db.execute(text("SELECT id FROM orgs"))).scalars().all()
    for org_id in org_ids:
        try:
            if count := await evaluate_rules(org_id):
                log.info("monitor alerts raised", extra={"org_id": str(org_id), "count": count})
        except Exception:  # noqa: BLE001 - one organisation's failure must not stop the others
            log.exception("monitor evaluation failed", extra={"org_id": str(org_id)})
