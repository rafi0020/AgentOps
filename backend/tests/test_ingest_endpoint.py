"""POST /v1/traces: what is accepted, what is refused, and that acceptance means stored."""

import gzip
import json
from datetime import UTC, datetime

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from sqlalchemy import text

from app.config import get_settings
from app.db import tenant_session
from tests.conftest import rows
from tests.otlp_helpers import make_batch, span


async def inbox(project) -> list:
    async with tenant_session(project.team.org_id) as db:
        return (
            await db.execute(
                text("SELECT span_count, status, content_type, project_id FROM ingest_batches ORDER BY received_at")
            )
        ).all()


@pytest.mark.parametrize(("encoding", "gz"), [("protobuf", False), ("json", False), ("protobuf", True), ("json", True)])
async def test_each_encoding_is_accepted_and_stored_before_the_reply(client, project, encoding, gz):
    body, headers = make_batch([span(), span(), span()], encoding=encoding, gzip=gz)
    r = await client.post("/v1/traces", content=body, headers=headers | project.auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(headers["Content-Type"])
    rows = await inbox(project)
    assert len(rows) == 1 and rows[0].span_count == 3 and rows[0].status == "pending"
    assert str(rows[0].project_id) == project.id


async def test_an_empty_export_is_acknowledged_without_storing_anything(client, project):
    body, headers = make_batch([])
    assert (await client.post("/v1/traces", content=body, headers=headers | project.auth)).status_code == 200
    assert await inbox(project) == []


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"\xff\xfe not protobuf at all \x00\x01", "application/x-protobuf"),
        (b"{not json", "application/json"),
        (b'{"resourceSpans": "not a list"}', "application/json"),
        (b"hello", "text/plain"),
    ],
)
async def test_malformed_bodies_are_refused_and_nothing_is_stored(client, project, body, content_type):
    r = await client.post("/v1/traces", content=body, headers={"Content-Type": content_type} | project.auth)
    assert r.status_code == 400 and await inbox(project) == []


async def test_a_span_that_ends_before_it_starts_is_dropped_and_the_rest_is_kept(client, project):
    bad = span(start=datetime.now(UTC), ms=-5)  # ends 5 ms before it starts
    body, headers = make_batch([span(), bad, span()], encoding="protobuf")
    r = await client.post("/v1/traces", content=body, headers=headers | project.auth)
    assert r.status_code == 200
    assert rejected_spans(r.content, "application/x-protobuf") == 1
    rows_ = await inbox(project)
    assert len(rows_) == 1 and rows_[0].span_count == 2


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
async def test_spans_with_bad_ids_or_timestamps_are_counted_as_rejected_never_500(client, project, encoding):
    body, headers = make_batch([span(), span(), span(), span()], encoding=encoding)  # one resource block per span
    if encoding == "json":
        doc = json.loads(body)
        blocks = doc["resourceSpans"]
        blocks[1]["scopeSpans"][0]["spans"][0]["traceId"] = "zz" * 16  # not hex
        blocks[2]["scopeSpans"][0]["spans"][0]["startTimeUnixNano"] = str(10**30)  # far outside any date
        blocks.append({"scopeSpans": [{"spans": [{"name": "no ids"}]}]})
        body = json.dumps(doc).encode()
    else:
        request = ExportTraceServiceRequest.FromString(body)
        blocks = request.resource_spans
        blocks[1].scope_spans[0].spans[0].trace_id = b"\x01"  # wrong length
        bad = blocks[2].scope_spans[0].spans[0]
        bad.start_time_unix_nano, bad.end_time_unix_nano = 2**64 - 1, 0  # ends before it starts
        blocks.add().scope_spans.add().spans.add()  # an empty span
        body = request.SerializeToString()
    r = await client.post("/v1/traces", content=body, headers=headers | project.auth)
    assert r.status_code == 200, r.text
    assert rejected_spans(r.content, headers["Content-Type"]) == 3
    assert sum(row.span_count for row in await inbox(project)) == 2


def rejected_spans(content: bytes, content_type: str) -> int:
    import json as _json

    if content_type.startswith("application/json"):
        return _json.loads(content).get("partialSuccess", {}).get("rejectedSpans", 0)
    response = ExportTraceServiceResponse.FromString(content)
    return response.partial_success.rejected_spans


async def test_oversized_and_decompression_bomb_bodies_are_refused(client, project):
    big, headers = b"x" * (4 * 1024 * 1024 + 1), {"Content-Type": "application/json"}
    assert (await client.post("/v1/traces", content=big, headers=headers | project.auth)).status_code == 413
    bomb = gzip.compress(b"\x00" * (50 * 1024 * 1024))  # 50 MB of zeros in about 50 KB
    r = await client.post("/v1/traces", content=bomb, headers=headers | {"Content-Encoding": "gzip"} | project.auth)
    assert r.status_code == 413 and await inbox(project) == []
    assert (
        await client.post(
            "/v1/traces", content=b"not gzip", headers=headers | {"Content-Encoding": "gzip"} | project.auth
        )
    ).status_code == 400


async def test_missing_unknown_and_revoked_keys_are_refused(client, project):
    body, headers = make_batch([span()])
    assert (await client.post("/v1/traces", content=body, headers=headers)).status_code == 401
    assert (
        await client.post("/v1/traces", content=body, headers=headers | {"Authorization": "Bearer ak_deadbeef_nope"})
    ).status_code == 401
    assert (
        await client.post("/v1/traces", content=body, headers=headers | {"Authorization": "Basic abc"})
    ).status_code == 401
    assert (
        await client.post("/v1/traces", content=body, headers=headers | project.auth)
    ).status_code == 200  # now cached
    assert (await project.team.admin.delete(f"/api/projects/{project.id}/keys/{project.key_id}")).status_code == 204
    assert (await client.post("/v1/traces", content=body, headers=headers | project.auth)).status_code == 401  # at once
    assert len(await inbox(project)) == 1


async def test_a_key_without_the_ingest_scope_cannot_send(client, project):
    r = await project.team.admin.post(
        f"/api/projects/{project.id}/keys", json={"name": "reader", "scopes": ["prompts:read"]}
    )
    body, headers = make_batch([span()])
    sent = await client.post(
        "/v1/traces", content=body, headers=headers | {"Authorization": f"Bearer {r.json()['key']}"}
    )
    assert sent.status_code == 403 and await inbox(project) == []


async def test_a_key_writes_only_to_its_own_project(client, project, other_team):
    """The project comes from the key, never from the request, so there is no id to tamper with."""
    body, headers = make_batch([span(resource={"service.name": "x", "project_id": "someone-elses"})])
    await client.post("/v1/traces", content=body, headers=headers | project.auth)
    async with tenant_session(other_team.org_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM ingest_batches"))).scalar_one() == 0
    assert str((await inbox(project))[0].project_id) == project.id


async def test_requests_over_the_per_key_limit_get_429(client, project, monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_requests_per_minute", 3)
    body, headers = make_batch([span()])
    codes = [
        (await client.post("/v1/traces", content=body, headers=headers | project.auth)).status_code for _ in range(4)
    ]
    assert codes == [200, 200, 200, 429]


async def test_if_the_batch_cannot_be_stored_the_sender_is_told_to_retry(client, project, monkeypatch):
    from app.api import ingest

    async def broken(*args, **kwargs):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(ingest, "store_batch", broken)
    body, headers = make_batch([span()])
    r = await client.post("/v1/traces", content=body, headers=headers | project.auth)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"


async def test_ingestion_keeps_working_while_redis_is_down(client, project, monkeypatch):
    """Found by the final review: the key cache and the rate limiter needed Redis, so a Redis outage turned every
    export into a 500, which exporters do not retry. PostgreSQL is the source of truth; Redis is only a cache."""
    from redis.exceptions import ConnectionError as RedisDown

    class Down:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise RedisDown("redis is down")

            return fail

    for module in ("app.ingest.keys", "app.security.ratelimit", "app.ingest.inbox"):
        monkeypatch.setattr(f"{module}.get_redis", lambda: Down())
    body, headers = make_batch([span()])
    r = await client.post("/v1/traces", content=body, headers=headers | {"Authorization": f"Bearer {project.key}"})
    assert r.status_code == 200, r.text
    assert (await rows(project.team.org_id, "SELECT count(*) FROM ingest_batches"))[0][0] == 1


async def test_a_key_revoked_while_a_request_is_caching_it_is_not_served_from_the_cache(client, project, monkeypatch):
    """The race: a request reads the key, the revoke commits and clears the cache, then the request writes its
    copy back. Without a check after the write the revoked key would be served for the next minute."""
    from app.ingest import keys

    real_redis = keys.get_redis()
    revoked_during_cache_write = False

    class RevokeWhileCaching:
        def __getattr__(self, name):
            return getattr(real_redis, name)

        async def set(self, *args, **kwargs):
            nonlocal revoked_during_cache_write
            if not revoked_during_cache_write:
                revoked_during_cache_write = True
                r = await project.team.admin.delete(f"/api/projects/{project.id}/keys/{project.key_id}")
                assert r.status_code == 204
            return await real_redis.set(*args, **kwargs)

    monkeypatch.setattr(keys, "get_redis", lambda: RevokeWhileCaching())
    body, headers = make_batch([span()])
    send = lambda: client.post("/v1/traces", content=body, headers=headers | {"Authorization": f"Bearer {project.key}"})  # noqa: E731
    assert (await send()).status_code == 401  # the request that raced the revoke is refused too
    monkeypatch.undo()
    assert (await send()).status_code == 401
    assert await real_redis.get(keys._cache_key(keys.hash_key(project.key))) is None
