"""Online scoring: deterministic evaluators applied to a sample of live traffic as it is ingested.

The sample is chosen by hashing the trace id, so the same traces are scored however many times a batch is
processed, and a trace is either scored completely or not at all. Only spans that carry captured content are
scored. LLM judges are not allowed here: they would put a model call, its cost and its failures inside
ingestion.
"""

import hashlib
import json
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.evals.evaluators import evaluate
from app.ingest.writer import WriteResult, bucket_of

log = logging.getLogger("agentops.online")


def sampled(trace_id: str, rate: float) -> bool:
    return int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


SCORES = text("""
    INSERT INTO trace_scores (org_id, project_id, trace_id, span_id, evaluator_id, value, passed, reason)
    SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb))
        AS r(org_id uuid, project_id uuid, trace_id text, span_id text, evaluator_id uuid, value double precision, passed boolean, reason text)
    ON CONFLICT DO NOTHING
    RETURNING evaluator_id, value, trace_id, span_id
""")
ROLLUP = text("""
    INSERT INTO score_rollups_5m AS s (org_id, project_id, bucket, evaluator_id, count, sum)
    SELECT org_id, project_id, bucket, evaluator_id, count, sum FROM jsonb_to_recordset(CAST(:rows AS jsonb))
        AS r(org_id uuid, project_id uuid, bucket timestamptz, evaluator_id uuid, count int, sum double precision)
    ON CONFLICT (project_id, bucket, evaluator_id) DO UPDATE SET count = s.count + excluded.count, sum = s.sum + excluded.sum
""")


async def score_online(db: AsyncSession, org_id: UUID, project_id: UUID, rows: list[dict], result: WriteResult) -> None:
    """Ingest step, in the same transaction as the spans: score newly stored spans that have content."""
    if not result.new_rows:
        return
    evaluators = (
        await db.execute(
            text("""SELECT e.id, e.kind, e.config, o.sample_rate FROM online_evaluators o JOIN evaluators e ON e.id = o.evaluator_id
                    WHERE o.project_id = :p"""),
            {"p": project_id},
        )
    ).all()
    if not evaluators:
        return
    new = {(r.trace_id, r.span_id) for r in result.new_rows}  # by span: a resent span is not new, whatever its trace
    candidates = [
        r
        for r in rows
        if r["content"] and r["content"].get("output") is not None and (r["trace_id"], r["span_id"]) in new
    ]
    scores, starts = [], {}
    for row in candidates:
        for evaluator in evaluators:
            if not sampled(row["trace_id"], evaluator.sample_rate):
                continue
            try:
                s = evaluate(evaluator.kind, evaluator.config, output=row["content"]["output"], expected=None, latency_ms=row["duration_ms"],
                             cost_usd=float(row["cost_usd"]) if row["cost_usd"] else None)  # fmt: skip
            except Exception:  # noqa: BLE001 - scoring is secondary: a broken evaluator must never fail ingestion
                log.exception("online evaluator failed", extra={"evaluator": str(evaluator.id)})
                continue
            if s.invalid:
                continue
            scores.append({"org_id": str(org_id), "project_id": str(project_id), "trace_id": row["trace_id"], "span_id": row["span_id"],
                           "evaluator_id": str(evaluator.id), "value": s.value, "passed": s.passed, "reason": s.reason[:300]})  # fmt: skip
            starts[(row["trace_id"], row["span_id"])] = _parse(row["start_time"])
    if not scores:
        return
    inserted = (await db.execute(SCORES, {"rows": json.dumps(scores)})).all()  # only scores not already stored count
    buckets: dict[tuple, list[float]] = {}
    for r in inserted:
        buckets.setdefault((bucket_of(starts[(r.trace_id, r.span_id)]).isoformat(), str(r.evaluator_id)), []).append(
            r.value
        )
    if buckets:
        payload = [{"org_id": str(org_id), "project_id": str(project_id), "bucket": b, "evaluator_id": e, "count": len(v), "sum": sum(v)}
                   for (b, e), v in buckets.items()]  # fmt: skip
        await db.execute(ROLLUP, {"rows": json.dumps(payload)})


def _parse(value):
    from datetime import datetime

    return datetime.fromisoformat(value) if isinstance(value, str) else value
