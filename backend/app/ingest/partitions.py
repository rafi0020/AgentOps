"""Daily partitions of the spans table.

One partition per day means old data is removed by dropping a whole table, which is instant and leaves no
bloat, and a query for a time range touches only the days it needs. A span whose day has no partition (far in
the past or the future) goes to the default partition rather than being refused.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from app.db import system_session


async def ensure_partitions(days_back: int = 35, days_ahead: int = 2, today: date | None = None) -> int:
    """Create any missing partitions in the window. Returns how many were created. Safe to run concurrently."""
    today = today or datetime.now(UTC).date()
    created = 0
    async with system_session() as db:
        for offset in range(-days_back, days_ahead + 1):
            made = await db.scalar(text("SELECT ensure_span_partition(:day)"), {"day": today + timedelta(days=offset)})
            created += bool(made)
    return created
