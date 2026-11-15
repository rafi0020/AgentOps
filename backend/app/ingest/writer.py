"""Turns received spans into rows and writes them idempotently.

A span is identified by (project, trace id, span id). Writing one that is already stored does nothing, so a
batch that arrives twice (an exporter retrying after a slow reply) or is processed twice (a worker restarted)
leaves exactly one copy. After spans are written, the summary row of every trace they belong to is
recomputed from all of that trace's spans, so spans may arrive in any order and across several batches.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.otlp import RawSpan
from app.ingest.pricing import Prices, cost_for
from app.security.redact import redact

MAX_ATTRIBUTE_CHARS = 4000
CONTENT_INPUT = ("gen_ai.input.messages", "gen_ai.prompt", "gen_ai.tool.call.arguments", "input.value")
CONTENT_OUTPUT = ("gen_ai.output.messages", "gen_ai.completion", "gen_ai.tool.call.result", "output.value")
BUCKET_SECONDS = 300


@dataclass
class WriteResult:
    inserted: int = 0
    duplicates: int = 0
    traces_touched: set[str] = field(default_factory=set)
    buckets_touched: set[datetime] = field(default_factory=set)
    new_rows: list = field(default_factory=list)  # the spans that were actually inserted, for roll-ups


def bucket_of(t: datetime) -> datetime:
    return datetime.fromtimestamp(int(t.timestamp()) // BUCKET_SECONDS * BUCKET_SECONDS, UTC)


def _first(attributes: dict, *names: str):
    return next((attributes[n] for n in names if attributes.get(n) not in (None, "")), None)


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _trim(value):
    if isinstance(value, str) and len(value) > MAX_ATTRIBUTE_CHARS:
        return value[:MAX_ATTRIBUTE_CHARS] + "…"
    return value


INT32 = 2**31 - 1
SHORT_TEXT = 500  # columns that are indexed or grouped on; a btree row must stay well under 2.7 KB
SHORT_COLUMNS = ("name", "operation", "model", "tool", "provider", "prompt_name", "service_name", "service_version",
                 "session_id")  # fmt: skip


def _no_nul(value):
    """PostgreSQL text and jsonb cannot hold U+0000; one such character would reject the whole batch."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    return value


def _clean(row: dict) -> dict:
    """Values a sender may get wrong are cleaned, never allowed to lose the batch they arrived in."""
    row = _no_nul(row)
    for column in SHORT_COLUMNS:
        if isinstance(row.get(column), str) and len(row[column]) > SHORT_TEXT:
            row[column] = row[column][:SHORT_TEXT]
    for column in ("input_tokens", "output_tokens", "prompt_version"):
        if row.get(column) is not None:
            row[column] = max(-INT32, min(INT32, row[column]))
    return row


def normalise(raw: RawSpan, org_id: UUID, project_id: UUID, prices: Prices) -> dict:
    """One span as a row. Understands the current GenAI attribute names and the ones they replaced."""
    a = dict(raw.attributes)
    model = _first(a, "gen_ai.response.model", "gen_ai.request.model")
    input_tokens = _int(_first(a, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"))
    output_tokens = _int(_first(a, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"))
    content = {}
    if (value := _first(a, *CONTENT_INPUT)) is not None:
        content["input"] = value
    if (value := _first(a, *CONTENT_OUTPUT)) is not None:
        content["output"] = value
    for name in (*CONTENT_INPUT, *CONTENT_OUTPUT):
        a.pop(name, None)
    cost = cost_for(prices, model, input_tokens, output_tokens)
    return _clean(
        {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "trace_id": raw.trace_id,
            "span_id": raw.span_id,
            "parent_span_id": raw.parent_span_id,
            "name": raw.name[:300] or "(unnamed)",
            "kind": raw.kind,
            "start_time": raw.start.isoformat(),
            "end_time": raw.end.isoformat(),
            "duration_ms": round((raw.end - raw.start).total_seconds() * 1000, 3),
            "status": raw.status,
            "status_message": redact(raw.status_message[:2000]) if raw.status_message else None,
            "operation": _first(a, "gen_ai.operation.name"),
            "model": model,
            "tool": _first(a, "gen_ai.tool.name"),
            "provider": _first(a, "gen_ai.provider.name", "gen_ai.system"),
            "prompt_name": _first(a, "agentops.prompt.name"),
            "prompt_version": _int(_first(a, "agentops.prompt.version")),
            "service_name": _first(raw.resource, "service.name"),
            "service_version": _first(raw.resource, "service.version"),
            "session_id": _first(a, "session.id") or _first(raw.resource, "session.id"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": str(cost) if cost is not None else None,
            # Masked on arrival as well as by a well-behaved sender: nothing secret-looking is stored.
            "attributes": redact({k: _trim(v) for k, v in a.items()}),
            "content": redact({k: _trim(v) for k, v in content.items()}) or None,
            "start_day": raw.start.date().isoformat(),
        }
    )


INSERT_SPANS = text("""
    INSERT INTO spans
    SELECT * FROM jsonb_populate_recordset(NULL::spans, CAST(:rows AS jsonb))
    ON CONFLICT DO NOTHING
    RETURNING trace_id, span_id, start_time, parent_span_id, status, duration_ms, model, operation, tool, prompt_name,
              prompt_version, service_version, input_tokens, output_tokens, cost_usd
""")

LOCK_TRACES = text("""
    SELECT count(pg_advisory_xact_lock(k)) FROM (
        SELECT DISTINCT hashtextextended(:project || ':' || t, 0) AS k FROM unnest(CAST(:traces AS text[])) AS t ORDER BY k
    ) AS keys
""")

# Recomputed from every span of the trace, whichever batch each came in.
UPSERT_TRACES = text("""
    INSERT INTO traces (org_id, project_id, trace_id, root_name, status, start_time, end_time, duration_ms,
                        span_count, error_count, input_tokens, output_tokens, cost_usd, models, tools,
                        session_id, service_name, service_version)
    SELECT org_id, project_id, trace_id,
           (array_agg(name ORDER BY (parent_span_id IS NULL) DESC, start_time))[1],
           CASE WHEN count(*) FILTER (WHERE status = 'error') > 0 THEN 'error' ELSE 'ok' END,
           min(start_time), max(end_time), extract(epoch FROM max(end_time) - min(start_time)) * 1000,
           count(*), count(*) FILTER (WHERE status = 'error'),
           coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), coalesce(sum(cost_usd), 0),
           coalesce(array_agg(DISTINCT model) FILTER (WHERE model IS NOT NULL), '{}'),
           coalesce(array_agg(DISTINCT tool) FILTER (WHERE tool IS NOT NULL), '{}'),
           max(session_id), max(service_name), max(service_version)
    FROM spans WHERE project_id = :project AND trace_id = ANY(:traces)
    GROUP BY org_id, project_id, trace_id
    ON CONFLICT (project_id, trace_id) DO UPDATE SET
        root_name = excluded.root_name, status = excluded.status, start_time = excluded.start_time,
        end_time = excluded.end_time, duration_ms = excluded.duration_ms, span_count = excluded.span_count,
        error_count = excluded.error_count, input_tokens = excluded.input_tokens,
        output_tokens = excluded.output_tokens, cost_usd = excluded.cost_usd, models = excluded.models,
        tools = excluded.tools, session_id = excluded.session_id, service_name = excluded.service_name,
        service_version = excluded.service_version
""")


async def write_spans(db: AsyncSession, project_id: UUID, rows: list[dict]) -> WriteResult:
    """Insert rows and refresh trace summaries, inside the caller's transaction."""
    result = WriteResult()
    if not rows:
        return result
    # One writer per trace at a time: the summary below is computed from the spans this transaction can see,
    # so a second batch of the same trace must wait until the first has committed. Locks are taken in key order
    # so two batches sharing several traces cannot deadlock; they are released at commit.
    await db.execute(LOCK_TRACES, {"project": str(project_id), "traces": sorted({r["trace_id"] for r in rows})})
    inserted = (await db.execute(INSERT_SPANS, {"rows": json.dumps(rows, default=str)})).all()
    result.inserted, result.duplicates = len(inserted), len(rows) - len(inserted)
    result.new_rows = inserted
    result.traces_touched = {r.trace_id for r in inserted}
    result.buckets_touched = {bucket_of(r.start_time) for r in inserted}
    if result.traces_touched:
        await db.execute(UPSERT_TRACES, {"project": project_id, "traces": list(result.traces_touched)})
    return result
