"""OTLP/HTTP trace ingestion:  POST /v1/traces

Any OpenTelemetry SDK can send here by setting its OTLP endpoint to this server and adding the header
`Authorization: Bearer <ingestion key>`.
"""

import json
import logging
import zlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from app.config import get_settings
from app.ingest.inbox import store_batch, wake_ingest_workers
from app.ingest.keys import KeyPrincipal, require_scope
from app.ingest.otlp import JSON, PROTOBUF, InvalidPayload, parse_request
from app.security.ratelimit import enforce

router = APIRouter(tags=["ingest"])
log = logging.getLogger("agentops.ingest")
MAX_BODY = 4 * 1024 * 1024


def _inflate(body: bytes) -> bytes:
    """Decompress with a hard ceiling, so a small request cannot expand into gigabytes."""
    inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        data = inflater.decompress(body, MAX_BODY + 1)
    except zlib.error as exc:
        raise HTTPException(400, "The body is not valid gzip.") from exc
    if len(data) > MAX_BODY or inflater.unconsumed_tail:
        raise HTTPException(413, "The batch is larger than 4 MB once decompressed. Send smaller batches.")
    return data


@router.post("/v1/traces")
async def receive_traces(request: Request, key: KeyPrincipal = Depends(require_scope("ingest"))) -> Response:
    # Fails open: a Redis outage must not turn away telemetry that PostgreSQL can store.
    await enforce(f"ingest:{key.key_id}", get_settings().ingest_requests_per_minute, 60, fail_open=True)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(413, "The batch is larger than 4 MB. Send smaller batches.")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "The batch is larger than 4 MB. Send smaller batches.")
    if "gzip" in request.headers.get("content-encoding", "").lower():
        body = _inflate(body)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        batch = parse_request(body, content_type)
    except InvalidPayload as exc:
        raise HTTPException(400, str(exc)) from exc
    if batch.spans:
        try:
            await store_batch(key.org_id, key.project_id, body, content_type, len(batch.spans))
        except Exception as exc:  # noqa: BLE001 - never say "accepted" for something that was not stored
            log.exception("inbox write failed")
            raise HTTPException(503, "The batch could not be stored. Retry.", headers={"Retry-After": "5"}) from exc
        await wake_ingest_workers()
    return _acknowledge(batch, content_type)


def _acknowledge(batch, content_type: str) -> Response:
    """The OTLP response in the sender's encoding. Dropped spans are reported as a partial success, not an error."""
    message = "; ".join(batch.reasons)
    if content_type == JSON:
        body = {"partialSuccess": {"rejectedSpans": batch.rejected, "errorMessage": message}} if batch.rejected else {}
        return Response(content=json.dumps(body).encode(), media_type=JSON)
    reply = ExportTraceServiceResponse()
    if batch.rejected:
        reply.partial_success.rejected_spans = batch.rejected
        reply.partial_success.error_message = message
    return Response(content=reply.SerializeToString(), media_type=PROTOBUF)


# ---- batches that could not be processed (administrators)

from uuid import UUID  # noqa: E402

from sqlalchemy import delete, func, select, update  # noqa: E402

from app.api.deps import audit, require_role  # noqa: E402
from app.db import tenant_session  # noqa: E402
from app.models.projects import IngestBatch  # noqa: E402
from app.security.sessions import Principal  # noqa: E402

admin_router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@admin_router.get("/dead")
async def dead_batches(admin: Principal = Depends(require_role("admin"))) -> dict:
    """Batches that failed five times. They are kept, not dropped, until someone replays or discards them."""
    async with tenant_session(admin.org_id) as db:
        rows = (
            await db.execute(
                select(
                    IngestBatch.id,
                    IngestBatch.project_id,
                    IngestBatch.span_count,
                    IngestBatch.error,
                    IngestBatch.received_at,
                )
                .where(IngestBatch.status == "dead")
                .order_by(IngestBatch.received_at.desc())
                .limit(200)
            )
        ).all()
        pending = await db.scalar(select(func.count()).select_from(IngestBatch).where(IngestBatch.status != "dead"))
    return {
        "pending": pending,
        "dead": [
            {"id": str(r.id), "project_id": str(r.project_id), "span_count": r.span_count, "error": r.error,
             "received_at": r.received_at.isoformat()}
            for r in rows
        ],
    }  # fmt: skip


@admin_router.post("/dead/{batch_id}/replay")
async def replay(batch_id: UUID, request: Request, admin: Principal = Depends(require_role("admin"))) -> dict:
    async with tenant_session(admin.org_id) as db:
        result = await db.execute(
            update(IngestBatch)
            .where(IngestBatch.id == batch_id, IngestBatch.status == "dead")
            .values(status="pending", attempts=0, error=None, lease_owner=None, lease_expires_at=None)
        )
        if result.rowcount != 1:
            raise HTTPException(404, "No dead batch with this id.")
        audit(db, admin, "ingest.replayed", str(batch_id), request=request)
    await wake_ingest_workers()
    return {"status": "pending"}


@admin_router.post("/dead/{batch_id}/discard", status_code=204)
async def discard(batch_id: UUID, request: Request, admin: Principal = Depends(require_role("admin"))) -> None:
    async with tenant_session(admin.org_id) as db:
        result = await db.execute(delete(IngestBatch).where(IngestBatch.id == batch_id, IngestBatch.status == "dead"))
        if result.rowcount != 1:
            raise HTTPException(404, "No dead batch with this id.")
        audit(db, admin, "ingest.discarded", str(batch_id), request=request)
