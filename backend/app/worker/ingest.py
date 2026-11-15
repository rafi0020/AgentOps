"""Ingest worker: moves received batches from the inbox into span storage.

    python -m app.worker.ingest --concurrency 4

Parsing a batch, inserting its spans, refreshing trace summaries and roll-ups, and deleting the inbox row all
happen in one transaction. A worker that dies at any point leaves the batch exactly as it was; another worker
claims it when the lease runs out. A batch that keeps failing is kept, marked dead, and shown to an admin.
"""

import argparse
import asyncio
import logging
import os
import signal
import socket
import uuid
from uuid import UUID

from sqlalchemy import delete, select, text

from app.config import get_settings
from app.db import dispose_engines, system_session, tenant_session
from app.ingest.inbox import WAKE_KEY, unpack
from app.ingest.otlp import parse_request
from app.ingest.partitions import ensure_partitions
from app.ingest.pricing import load_prices
from app.ingest.rollups import add_to_rollups
from app.ingest.writer import WriteResult, normalise, write_spans
from app.models.projects import IngestBatch
from app.monitors.online import score_online
from app.obs.logging import configure_logging
from app.redis import close_redis, get_redis

log = logging.getLogger("agentops.ingest")
MAX_ATTEMPTS = 5

# A batch whose worker died (the process was killed, so no failure handler ran) and that has used up its
# attempts is marked dead here; it is never handed to another worker, which would die the same way.
ABANDON = text("""
UPDATE ingest_batches SET status = 'dead', lease_owner = NULL, lease_expires_at = NULL,
       error = 'abandoned: the worker holding it stopped after ' || attempts::text || ' attempts'
WHERE status = 'processing' AND lease_expires_at <= now() AND attempts >= :max
""")

CLAIM = text("""
WITH candidate AS (
    SELECT id FROM ingest_batches
    WHERE status <> 'dead' AND attempts < :max AND (lease_expires_at IS NULL OR lease_expires_at <= now())
    ORDER BY received_at LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE ingest_batches b SET status = 'processing', lease_owner = :worker, attempts = b.attempts + 1,
       lease_expires_at = now() + make_interval(secs => :lease)
FROM candidate WHERE b.id = candidate.id
RETURNING b.id, b.org_id, b.attempts
""")

# Steps that commit together with the spans. Online scoring adds itself here too.
AFTER_WRITE: list = [add_to_rollups, score_online]


async def claim_batch(worker_id: str, lease_s: int = 30):
    async with system_session() as db:
        await db.execute(ABANDON, {"max": MAX_ATTEMPTS})
        return (await db.execute(CLAIM, {"worker": worker_id, "lease": lease_s, "max": MAX_ATTEMPTS})).first()


async def process_batch(batch_id: UUID, org_id: UUID) -> WriteResult:
    """Everything for one batch, in one transaction."""
    async with tenant_session(org_id) as db:
        batch = (await db.execute(select(IngestBatch).where(IngestBatch.id == batch_id))).scalar_one_or_none()
        if batch is None:
            return WriteResult()  # finished by another worker
        spans = parse_request(unpack(batch), batch.content_type).spans  # rejected spans were counted at arrival
        prices = await load_prices(db, org_id)
        rows = [normalise(s, org_id, batch.project_id, prices) for s in spans]
        result = await write_spans(db, batch.project_id, rows)
        for step in AFTER_WRITE:
            await step(db, org_id, batch.project_id, rows, result)
        await db.execute(delete(IngestBatch).where(IngestBatch.id == batch_id))
        if pause := os.environ.get("AGENTOPS_TEST_PAUSE_BEFORE_COMMIT"):  # lets a test kill the worker right here
            await asyncio.sleep(float(pause))
    return result


async def handle(claimed, worker_id: str) -> None:
    try:
        result = await process_batch(claimed.id, claimed.org_id)
        log.info(
            "batch stored",
            extra={"batch": str(claimed.id), "inserted": result.inserted, "duplicates": result.duplicates},
        )
    except Exception as exc:  # noqa: BLE001 - the batch stays in the inbox; decide between retry and dead
        log.exception("batch failed", extra={"batch": str(claimed.id), "attempt": claimed.attempts})
        dead = claimed.attempts >= MAX_ATTEMPTS
        async with system_session() as db:
            await db.execute(
                text(
                    "UPDATE ingest_batches SET status = :status, error = :error, lease_owner = NULL, "
                    "lease_expires_at = CASE WHEN :dead THEN NULL ELSE now() + make_interval(secs => :wait) END "
                    "WHERE id = :id AND lease_owner = :worker"
                ),
                {"status": "dead" if dead else "pending", "error": f"{type(exc).__name__}: {exc}"[:500], "dead": dead,
                 "wait": 2 * claimed.attempts, "id": claimed.id, "worker": worker_id},
            )  # fmt: skip


async def drain(worker_id: str = "test-ingest") -> int:
    """Process everything currently claimable. Used by tests and by the demo data loader."""
    n = 0
    while (claimed := await claim_batch(worker_id)) is not None:
        await handle(claimed, worker_id)
        n += 1
    return n


async def serve(concurrency: int, stop: asyncio.Event, poll_s: float = 1.0) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    await ensure_partitions()
    log.info("ingest worker started", extra={"worker": worker_id, "concurrency": concurrency})
    tasks: set[asyncio.Task] = set()
    last_partition_check = asyncio.get_running_loop().time()
    while not stop.is_set():
        tasks = {t for t in tasks if not t.done()}
        if len(tasks) >= concurrency:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            continue
        try:
            claimed = await claim_batch(worker_id)
        except Exception:  # noqa: BLE001 - e.g. the database is restarting
            log.exception("claim failed; retrying")
            await asyncio.sleep(poll_s)
            continue
        if claimed is None:
            if asyncio.get_running_loop().time() - last_partition_check > 3600:
                await ensure_partitions()
                last_partition_check = asyncio.get_running_loop().time()
            try:
                await get_redis().blpop([WAKE_KEY], timeout=poll_s)
            except Exception:  # noqa: BLE001 - fall back to polling if Redis is away
                await asyncio.sleep(poll_s)
            continue
        tasks.add(asyncio.create_task(handle(claimed, worker_id)))
    await asyncio.gather(*tasks, return_exceptions=True)  # let in-flight batches finish; they are short


async def main(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await serve(args.concurrency, stop)
    finally:
        await close_redis()
        await dispose_engines()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgentOps ingest worker")
    parser.add_argument("--concurrency", type=int, default=4)
    configure_logging(get_settings().log_level)
    asyncio.run(main(parser.parse_args()))
