"""Ingest workers: each batch once, failures kept and replayable."""

import asyncio

from sqlalchemy import text

from app.db import system_session, tenant_session
from app.ingest.partitions import ensure_partitions
from app.worker import ingest
from app.worker.ingest import claim_batch, drain, handle
from tests.conftest import rows
from tests.otlp_helpers import make_batch, span


async def post(client, project, n=1):
    body, headers = make_batch([span() for _ in range(n)])
    assert (await client.post("/v1/traces", content=body, headers=headers | project.auth)).status_code == 200


async def test_concurrent_workers_process_each_batch_exactly_once(client, project):
    await ensure_partitions(days_back=2, days_ahead=1)
    for _ in range(40):
        await post(client, project, 3)
    counts = await asyncio.gather(*[drain(f"w{i}") for i in range(8)])
    assert sum(counts) >= 40  # other tests may have left batches in the shared inbox; the checks below are exact
    assert (await rows(project.team.org_id, "SELECT count(*) FROM spans"))[0][0] == 120
    assert (await rows(project.team.org_id, "SELECT count(*) FROM ingest_batches"))[0][0] == 0


async def test_a_batch_that_keeps_failing_is_kept_as_dead_and_can_be_replayed(client, project, monkeypatch):
    team = project.team
    await ensure_partitions(days_back=2, days_ahead=1)
    await post(client, project, 2)

    async def broken(*args, **kwargs):
        raise RuntimeError("storage exploded")

    real = ingest.write_spans
    monkeypatch.setattr(ingest, "write_spans", broken)
    for attempt in range(5):
        async with system_session() as db:  # skip the back-off wait between attempts
            await db.execute(text("UPDATE ingest_batches SET lease_expires_at = NULL"))
        claimed = await claim_batch("w")
        assert claimed.attempts == attempt + 1
        await handle(claimed, "w")
    assert await claim_batch("w") is None  # dead batches are not retried
    listed = (await team.admin.get("/api/ingest/dead")).json()
    assert listed["pending"] == 0 and len(listed["dead"]) == 1
    assert listed["dead"][0]["span_count"] == 2 and "storage exploded" in listed["dead"][0]["error"]
    assert (await team.engineer.get("/api/ingest/dead")).status_code == 403

    monkeypatch.setattr(ingest, "write_spans", real)  # the fault is fixed
    batch_id = listed["dead"][0]["id"]
    assert (await team.admin.post(f"/api/ingest/dead/{batch_id}/replay")).json() == {"status": "pending"}
    assert await drain() == 1
    assert (await rows(team.org_id, "SELECT count(*) FROM spans"))[0][0] == 2
    assert (await team.admin.get("/api/ingest/dead")).json()["dead"] == []


async def test_a_dead_batch_can_be_discarded_and_other_orgs_cannot_touch_it(client, project, other_team):
    await post(client, project)
    async with tenant_session(project.team.org_id) as db:
        batch_id = (await db.execute(text("UPDATE ingest_batches SET status = 'dead' RETURNING id"))).scalar_one()
    assert (await other_team.admin.post(f"/api/ingest/dead/{batch_id}/discard")).status_code == 404
    assert (await other_team.admin.post(f"/api/ingest/dead/{batch_id}/replay")).status_code == 404
    assert (await project.team.admin.post(f"/api/ingest/dead/{batch_id}/discard")).status_code == 204
    assert (await rows(project.team.org_id, "SELECT count(*) FROM ingest_batches"))[0][0] == 0


async def test_a_batch_that_kills_every_worker_is_marked_dead_instead_of_claimed_forever(client, project):
    """A batch that crashes the process never reaches the failure handler, so the cap must hold in the claim."""
    await ensure_partitions(days_back=2, days_ahead=1)
    async with system_session() as db:
        await db.execute(text("UPDATE ingest_batches SET status = 'dead' WHERE status <> 'dead'"))
    await post(client, project, 2)
    async with system_session() as db:  # the last worker died holding it on its final allowed attempt
        await db.execute(
            text(
                "UPDATE ingest_batches SET status = 'processing', attempts = :n, "
                "lease_expires_at = now() - interval '1 second' WHERE span_count = 2"
            ),
            {"n": ingest.MAX_ATTEMPTS},
        )
    assert await claim_batch("w") is None
    async with system_session() as db:
        status, error = (
            await db.execute(
                text("SELECT status, error FROM ingest_batches WHERE span_count = 2 ORDER BY received_at DESC LIMIT 1")
            )
        ).one()
    assert status == "dead" and "attempts" in error
