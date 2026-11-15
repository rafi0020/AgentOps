"""Ingestion load: how many spans per second are accepted and written, and what happens when senders outrun
the ingest workers.

    uv run python -m load.ingest_load --host http://localhost:3200 --seconds 60 --senders 8 --batch 200

Every span is new (fresh trace and span ids), so the idempotent write cannot skip duplicates. Batches are
gzip-compressed protobuf, as OpenTelemetry exporters send them. While senders run, the inbox depth is sampled
from the database every two seconds; after they stop, sampling continues until the inbox is empty, so the
write rate is measured end to end (accepted spans / time until all of them were written).

The script creates its own project and ingestion key in the target database (owner connection).
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import secrets
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from app.api.health import libpq_url
from seed.demo_users import LUMEN

RESULTS = Path(__file__).parent / "results"
OPERATIONS = [("chat", "model-fast"), ("execute_tool", None), ("chat", "model-large"), ("execute_tool", None)]


def owner(url: str, sql: str, params: tuple = ()):
    with psycopg.connect(url) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.system', 'on', true)")
        conn.execute("SELECT set_config('app.org_id', %s, true)", (str(LUMEN),))
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else None


def ensure_key(url: str, project: str) -> str:
    """A project and a fresh ingestion key for the load test; returns the plain key."""
    owner(
        url,
        "INSERT INTO projects (org_id, name, retention_days) VALUES (%s, %s, 30) ON CONFLICT DO NOTHING",
        (LUMEN, project),
    )
    prefix = secrets.token_hex(4)
    plain = f"ak_{prefix}_{secrets.token_urlsafe(24)}"
    owner(
        url,
        "INSERT INTO ingest_keys (org_id, project_id, name, key_hash, prefix) "
        "SELECT org_id, id, 'load test', %s, %s FROM projects WHERE org_id = %s AND name = %s",
        (hashlib.sha256(plain.encode()).hexdigest(), f"ak_{prefix}", LUMEN, project),
    )
    return plain


def _kv(key: str, value) -> KeyValue:
    v = AnyValue(int_value=value) if isinstance(value, int) else AnyValue(string_value=str(value))
    return KeyValue(key=key, value=v)


def batch(size: int) -> bytes:
    """`size` new spans: traces of one agent span with child model and tool spans, ending just now."""
    request = ExportTraceServiceRequest()
    rs = request.resource_spans.add()
    rs.resource.attributes.extend([_kv("service.name", "load-app"), _kv("service.version", "1.0")])
    spans = rs.scope_spans.add().spans
    now = datetime.now(UTC)
    while len(spans) < size:
        trace, root = secrets.token_bytes(16), secrets.token_bytes(8)
        start = now - timedelta(milliseconds=2000)
        s = spans.add()
        s.trace_id, s.span_id, s.name, s.kind = trace, root, "invoke_agent load-app", 1
        s.start_time_unix_nano, s.end_time_unix_nano = int(start.timestamp() * 1e9), int(now.timestamp() * 1e9)
        s.attributes.extend([_kv("gen_ai.operation.name", "invoke_agent"), _kv("session.id", secrets.token_hex(6))])
        for i, (operation, model) in enumerate(OPERATIONS[: min(len(OPERATIONS), size - len(spans))]):
            c = spans.add()
            c.trace_id, c.span_id, c.parent_span_id, c.kind = trace, secrets.token_bytes(8), root, 1
            begin = start + timedelta(milliseconds=400 * i)
            c.start_time_unix_nano = int(begin.timestamp() * 1e9)
            c.end_time_unix_nano = int((begin + timedelta(milliseconds=350)).timestamp() * 1e9)
            attrs = [_kv("gen_ai.operation.name", operation)]
            if model:
                c.name = f"chat {model}"
                attrs += [
                    _kv("gen_ai.request.model", model),
                    _kv("gen_ai.usage.input_tokens", 900),
                    _kv("gen_ai.usage.output_tokens", 120),
                ]
            else:
                c.name = "execute_tool lookup_order"
                attrs += [_kv("gen_ai.tool.name", "lookup_order")]
            c.attributes.extend(attrs)
    return gzip.compress(request.SerializeToString())


async def main(args: argparse.Namespace) -> None:
    url = libpq_url(args.owner_url)
    key = ensure_key(url, args.project)
    (project_id,) = owner(url, "SELECT id FROM projects WHERE org_id = %s AND name = %s", (LUMEN, args.project))[0]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/x-protobuf", "Content-Encoding": "gzip"}
    accepted = rejected = 0
    statuses: dict[int, int] = {}
    latencies: list[float] = []
    samples: list[dict] = []
    began = time.perf_counter()
    stop_at = began + args.seconds

    def depth() -> tuple[int, int]:
        rows = owner(url, "SELECT status, count(*) FROM ingest_batches WHERE project_id = %s GROUP BY 1", (project_id,))
        counts = dict(rows or [])
        return counts.get("pending", 0) + counts.get("processing", 0), counts.get("dead", 0)

    async def sender(client: httpx.AsyncClient) -> None:
        nonlocal accepted, rejected
        loop = asyncio.get_running_loop()
        while time.perf_counter() < stop_at:
            body = await loop.run_in_executor(None, batch, args.batch)
            t = time.perf_counter()
            try:
                r = await client.post("/v1/traces", content=body, headers=headers)
                code = r.status_code
            except httpx.HTTPError:
                code = 0
            latencies.append(time.perf_counter() - t)
            statuses[code] = statuses.get(code, 0) + 1
            if code == 200:
                accepted += args.batch
            else:
                rejected += args.batch
                await asyncio.sleep(0.5)  # an exporter backs off on 429 and 503

    async def sampler() -> None:
        while True:
            pending, dead = await asyncio.to_thread(depth)
            elapsed = time.perf_counter() - began
            samples.append({"t": round(elapsed, 1), "accepted_spans": accepted, "inbox_batches": pending, "dead": dead})
            print(f"  t={elapsed:5.0f}s accepted={accepted:>9,} inbox={pending:>6,} batches")
            if time.perf_counter() > stop_at and pending == 0:
                return
            if time.perf_counter() > stop_at + args.drain_limit:
                print("  drain limit reached; inbox not empty")
                return
            await asyncio.sleep(2)

    async with httpx.AsyncClient(
        base_url=args.host, timeout=30, limits=httpx.Limits(max_connections=args.senders)
    ) as client:
        watcher = asyncio.create_task(sampler())
        await asyncio.gather(*[sender(client) for _ in range(args.senders)])
        sent_for = time.perf_counter() - began
        await watcher
    drained = samples[-1]["t"]
    lat = sorted(latencies)
    result = {
        "label": args.label, "at": datetime.now(UTC).isoformat(timespec="seconds"), "senders": args.senders, "batch": args.batch,
        "seconds_sending": round(sent_for, 1), "accepted_spans": accepted, "rejected_spans": rejected, "statuses": statuses,
        "accepted_per_s": round(accepted / sent_for), "written_per_s": round(accepted / drained) if samples[-1]["inbox_batches"] == 0 else None,
        "seconds_until_written": drained, "max_inbox_batches": max(s["inbox_batches"] for s in samples),
        "request_p50_ms": round(statistics.median(lat) * 1000, 1), "request_p95_ms": round(lat[int(len(lat) * 0.95)] * 1000, 1),
        "samples": samples,
    }  # fmt: skip
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"ingest-{args.label}.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingestion load test")
    parser.add_argument("--host", default="http://localhost:3200")
    parser.add_argument(
        "--owner-url", default="postgresql+psycopg://agentops_owner:agentops_owner@localhost:55442/agentops_load"
    )
    parser.add_argument("--project", default="load-test")
    parser.add_argument("--senders", type=int, default=8)
    parser.add_argument("--batch", type=int, default=200)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--drain-limit", type=int, default=600)
    parser.add_argument("--label", default="run")
    asyncio.run(main(parser.parse_args()))
