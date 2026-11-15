"""The inbox: a received batch is stored here, as it arrived, before the sender is told it was accepted.

That ordering is the durability guarantee. Once the sender has a success response the batch is in PostgreSQL,
so no restart of any component can lose it. Redis is only used to nudge an idle ingest worker.
"""

import zlib
from uuid import UUID

from app.db import tenant_session
from app.models.projects import IngestBatch
from app.redis import get_redis

WAKE_KEY = "ingest:wake"


async def store_batch(org_id: UUID, project_id: UUID, body: bytes, content_type: str, span_count: int) -> UUID:
    async with tenant_session(org_id) as db:
        batch = IngestBatch(
            org_id=org_id, project_id=project_id, payload=zlib.compress(body, 6), content_type=content_type,
            span_count=span_count,
        )  # fmt: skip
        db.add(batch)
        await db.flush()
        return batch.id


def unpack(batch: IngestBatch) -> bytes:
    return zlib.decompress(batch.payload)


async def wake_ingest_workers() -> None:
    try:
        redis = get_redis()
        pipe = redis.pipeline()
        pipe.lpush(WAKE_KEY, "1")
        pipe.ltrim(WAKE_KEY, 0, 63)
        pipe.expire(WAKE_KEY, 60)
        await pipe.execute()
    except Exception:  # noqa: BLE001, S110 - only a nudge; workers also poll
        pass
