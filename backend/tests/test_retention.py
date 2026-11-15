"""Retention removes what each project no longer keeps, and nothing else."""

from datetime import UTC, datetime, timedelta

from app.ingest.partitions import ensure_partitions
from app.ingest.retention import apply_retention
from tests.conftest import rows, send
from tests.otlp_helpers import span


async def test_old_data_goes_and_a_project_with_longer_retention_keeps_its_own(client, project):
    team = project.team
    keeper = (await team.admin.post("/api/projects", json={"name": "keeps-90-days", "retention_days": 90})).json()
    key = (await team.admin.post(f"/api/projects/{keeper['id']}/keys", json={"name": "k"})).json()["key"]
    await team.admin.patch(f"/api/projects/{project.id}", json={"retention_days": 30})
    old, recent = datetime.now(UTC) - timedelta(days=40), datetime.now(UTC) - timedelta(days=2)
    await ensure_partitions(days_back=45, days_ahead=1)
    await send(client, project, [span("old", start=old), span("recent", start=recent)])

    class Other:
        auth = {"Authorization": f"Bearer {key}"}

    await send(client, Other, [span("kept old", start=old)])
    result = await apply_retention()
    assert result["spans_trimmed"] >= 1
    assert sorted(r[0] for r in await rows(team.org_id, "SELECT name FROM spans")) == ["kept old", "recent"]
    assert sorted(r[0] for r in await rows(team.org_id, "SELECT root_name FROM traces")) == ["kept old", "recent"]
    buckets = await rows(team.org_id, "SELECT project_id::text, count(*) FROM rollups_5m GROUP BY 1")
    assert dict(buckets) == {project.id: 1, keeper["id"]: 1}


async def test_partitions_older_than_every_projects_retention_are_dropped():
    from sqlalchemy import text

    from app.db import system_session

    async with system_session() as db:
        keep_from = await db.scalar(text("SELECT min(current_date - retention_days) FROM projects"))
        ancient = keep_from - timedelta(days=400)
        assert await db.scalar(text("SELECT ensure_span_partition(:d)"), {"d": ancient}) is True
    assert (await apply_retention())["partitions_dropped"] >= 1
    async with system_session() as db:
        days = (await db.execute(text("SELECT span_partition_days()"))).scalars().all()
    assert ancient not in days and all(day >= keep_from for day in days)
