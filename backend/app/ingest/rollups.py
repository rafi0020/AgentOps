"""Five-minute roll-ups.

Charts, monitors and the Investigator never read raw spans: they read `rollups_5m`, one row per project,
five-minute bucket and combination of model, operation, tool, prompt version and service version.

Roll-ups are updated incrementally, in the same transaction that inserts the spans, and only from the spans
that were actually new. So they are exact (never ahead of or behind the spans), a batch that arrives twice
adds nothing, and a span that arrives late updates the bucket it belongs to.

Latency is stored as a histogram with fixed bounds rather than as percentiles, because histograms can be
added together and percentiles cannot. A percentile read from it is the upper bound of the bucket holding
that rank, so it is approximate to the width of the bucket.
"""

import json
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.writer import WriteResult, bucket_of

LATENCY_BOUNDS_MS = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000]
SLOTS = len(LATENCY_BOUNDS_MS) + 1  # the last slot holds everything above the highest bound
DIMENSIONS = ("model", "operation", "tool", "prompt_name", "prompt_version", "service_version")
STEPS = {"5m": "5 minutes", "1h": "1 hour", "1d": "1 day"}
# The histogram has a fixed number of slots, so it is summed slot by slot with the built-in sum(). The array
# aggregate sum_int_array gives the same answer but builds a new array per row: 0.9 s instead of about 0.1 s
# for 30 days of one organisation's roll-ups (docs/SCALE.md).
HIST_SUM = "ARRAY[" + ", ".join(f"coalesce(sum(latency_hist[{i}]), 0)" for i in range(1, SLOTS + 1)) + "] AS hist"


def slot_of(duration_ms: float) -> int:
    return bisect_left(LATENCY_BOUNDS_MS, duration_ms)


def merge(hists: list[list[int]]) -> list[int]:
    out = [0] * SLOTS
    for hist in hists:
        for i, n in enumerate(hist or []):
            out[i] += n
    return out


def percentile(hist: list[int] | None, q: float) -> float | None:
    """Upper bound, in ms, of the histogram slot containing the q-th call. None when there are no calls."""
    total = sum(hist or [])
    if total == 0:
        return None
    rank, seen = q * total, 0
    for i, n in enumerate(hist):
        seen += n
        if seen >= rank and n:
            return float(LATENCY_BOUNDS_MS[i]) if i < len(LATENCY_BOUNDS_MS) else float(LATENCY_BOUNDS_MS[-1]) * 2
    return float(LATENCY_BOUNDS_MS[-1]) * 2


UPSERT = text("""
    INSERT INTO rollups_5m AS r
    SELECT * FROM jsonb_populate_recordset(NULL::rollups_5m, CAST(:rows AS jsonb))
    ORDER BY project_id, bucket, model, operation, tool, prompt_name, prompt_version, service_version
    ON CONFLICT (project_id, bucket, model, operation, tool, prompt_name, prompt_version, service_version) DO UPDATE SET
        calls = r.calls + excluded.calls, errors = r.errors + excluded.errors, roots = r.roots + excluded.roots,
        input_tokens = r.input_tokens + excluded.input_tokens, output_tokens = r.output_tokens + excluded.output_tokens,
        cost_usd = r.cost_usd + excluded.cost_usd, latency_hist = int_array_add(r.latency_hist, excluded.latency_hist)
""")


def aggregate(org_id: UUID, project_id: UUID, spans: list) -> list[dict]:
    """Group newly inserted spans into roll-up rows."""
    groups: dict[tuple, dict] = defaultdict(
        lambda: {
            "calls": 0,
            "errors": 0,
            "roots": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": Decimal(0),
            "latency_hist": [0] * SLOTS,
        }
    )
    for s in spans:
        key = (bucket_of(s.start_time), s.model or "", s.operation or "", s.tool or "", s.prompt_name or "",
               s.prompt_version or 0, s.service_version or "")  # fmt: skip
        g = groups[key]
        g["calls"] += 1
        g["errors"] += s.status == "error"
        g["roots"] += s.parent_span_id is None
        g["input_tokens"] += s.input_tokens or 0
        g["output_tokens"] += s.output_tokens or 0
        g["cost_usd"] += s.cost_usd or Decimal(0)
        g["latency_hist"][slot_of(s.duration_ms)] += 1
    return [
        {"org_id": str(org_id), "project_id": str(project_id), "bucket": key[0].isoformat(), **dict(zip(DIMENSIONS, key[1:], strict=True)),
         **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in g.items()}}
        for key, g in groups.items()
    ]  # fmt: skip


async def add_to_rollups(
    db: AsyncSession, org_id: UUID, project_id: UUID, rows: list[dict], result: WriteResult
) -> None:
    """Ingest step: fold the spans this batch really inserted into their buckets."""
    if result.new_rows:
        await db.execute(UPSERT, {"rows": json.dumps(aggregate(org_id, project_id, result.new_rows))})


async def query_rollups(
    db: AsyncSession,
    project_id: UUID | None,
    start: datetime,
    end: datetime,
    group_by: list[str] | None = None,
    step: str | None = None,
    filters: dict | None = None,
) -> list[dict]:
    """Figures from roll-ups, grouped by any of the dimensions and optionally by time step.

    Each row has calls, errors, error_rate, roots, tokens, cost_usd, p50_ms and p95_ms. `project_id=None`
    covers every project of the organisation bound to the session.
    """
    group_by = [g for g in (group_by or []) if g in DIMENSIONS or g == "project_id"]
    filters = {k: v for k, v in (filters or {}).items() if k in DIMENSIONS and v is not None}
    select = [f"date_bin('{STEPS[step]}', bucket, TIMESTAMPTZ 'epoch') AS t"] if step else []
    select += group_by
    keys = (["t"] if step else []) + group_by
    where = ["bucket >= :start", "bucket < :end"] + [f"{k} = :f_{k}" for k in filters]
    if project_id is not None:
        where.append("project_id = :project")
    sql = (
        f"SELECT {', '.join(select) + ', ' if select else ''}"  # noqa: S608 - identifiers come from fixed tuples above
        "sum(calls) AS calls, sum(errors) AS errors, sum(roots) AS roots, sum(input_tokens) AS input_tokens, "
        "sum(output_tokens) AS output_tokens, sum(cost_usd) AS cost_usd, " + HIST_SUM + " "
        f"FROM rollups_5m WHERE {' AND '.join(where)} "
        + (f"GROUP BY {', '.join(keys)} ORDER BY {', '.join(keys)}" if keys else "")
    )
    params = {"start": start, "end": end, "project": project_id} | {f"f_{k}": v for k, v in filters.items()}
    out = []
    for r in (await db.execute(text(sql), params)).mappings():
        calls = int(r["calls"] or 0)
        row = {k: (r[k].isoformat() if k == "t" else (str(r[k]) if k == "project_id" else r[k])) for k in keys}
        row |= {
            "calls": calls,
            "errors": int(r["errors"] or 0),
            "error_rate": round(int(r["errors"] or 0) / calls, 6) if calls else None,
            "roots": int(r["roots"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "cost_usd": round(float(r["cost_usd"] or 0), 6),
            "p50_ms": percentile(r["hist"], 0.5),
            "p95_ms": percentile(r["hist"], 0.95),
        }
        out.append(row)
    return out
