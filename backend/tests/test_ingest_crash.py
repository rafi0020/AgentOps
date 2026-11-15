"""An ingest worker is killed after writing spans but before its transaction commits; another finishes."""

import asyncio
import os
import signal
import subprocess
import sys
import time

from sqlalchemy import text

from app.db import system_session
from app.ingest.partitions import ensure_partitions
from tests.conftest import rows
from tests.otlp_helpers import make_batch, span


def start_worker(pause: str) -> subprocess.Popen:
    env = os.environ | {"AGENTOPS_TEST_PAUSE_BEFORE_COMMIT": pause, "LOG_LEVEL": "WARNING"}
    return subprocess.Popen([sys.executable, "-m", "app.worker.ingest", "--concurrency", "1"], env=env)


async def wait_for(check, seconds: float = 25.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out waiting")


async def test_killed_ingest_worker_loses_nothing_and_duplicates_nothing(client, project):
    await ensure_partitions(days_back=2, days_ahead=1)
    async with system_session() as db:  # nothing left over from other tests for the worker to pick up first
        await db.execute(text("UPDATE ingest_batches SET status = 'dead' WHERE status <> 'dead'"))
    body, headers = make_batch([span() for _ in range(25)])
    assert (await client.post("/v1/traces", content=body, headers=headers | project.auth)).status_code == 200

    async def claimed() -> bool:
        async with system_session() as db:
            return (await db.scalar(text("SELECT count(*) FROM ingest_batches WHERE status = 'processing'"))) == 1

    first = start_worker(pause="60")
    try:
        await wait_for(claimed)
        await asyncio.sleep(0.5)  # it has written the spans and is paused just before committing
        first.send_signal(signal.SIGKILL)
        first.wait()
    finally:
        if first.poll() is None:
            first.kill()
    assert (await rows(project.team.org_id, "SELECT count(*) FROM spans"))[0][0] == 0  # nothing half-written

    async with system_session() as db:  # do not wait out the dead worker's lease
        await db.execute(text("UPDATE ingest_batches SET lease_expires_at = now() WHERE status = 'processing'"))
    second = start_worker(pause="0")
    try:

        async def stored() -> bool:
            return (await rows(project.team.org_id, "SELECT count(*) FROM spans"))[0][0] == 25

        await wait_for(stored)
    finally:
        second.send_signal(signal.SIGTERM)
        second.wait(timeout=10)
    assert (await rows(project.team.org_id, "SELECT count(*), count(DISTINCT span_id) FROM spans"))[0] == (25, 25)
    assert (await rows(project.team.org_id, "SELECT count(*) FROM ingest_batches WHERE status <> 'dead'"))[0][0] == 0
