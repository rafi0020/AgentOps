"""Span storage: idempotent, order-independent, and tolerant of spans that are not from an LLM."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.ingest.pricing import cost_for
from tests.conftest import rows, send
from tests.otlp_helpers import span, span_id, trace_id

LLM = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "model-fast", "gen_ai.provider.name": "acme",
       "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 200}  # fmt: skip


async def test_a_batch_sent_twice_is_stored_once(client, project):
    t = trace_id()
    batch = [span(trace=t, attrs=LLM), span("execute_tool lookup", trace=t, attrs={"gen_ai.tool.name": "lookup"})]
    await send(client, project, batch)
    before = await rows(project.team.org_id, "SELECT span_count, input_tokens FROM traces")
    await send(client, project, batch)  # an exporter retrying
    await send(client, project, batch, encoding="json")  # and again in the other encoding
    assert (await rows(project.team.org_id, "SELECT count(*) FROM spans"))[0][0] == 2
    assert await rows(project.team.org_id, "SELECT span_count, input_tokens FROM traces") == before == [(2, 1000)]
    assert await rows(project.team.org_id, "SELECT count(*) FROM ingest_batches") == [(0,)]


async def test_trace_summary_is_correct_whatever_order_spans_arrive_in(client, project):
    t, root, start = trace_id(), span_id(), datetime.now(UTC) - timedelta(minutes=5)
    child = span(
        "execute_tool lookup",
        trace=t,
        parent=root,
        start=start + timedelta(milliseconds=50),
        ms=100,
        error="validation error",
    )
    await send(client, project, [child])  # the child first, its parent not yet seen
    first = (await rows(project.team.org_id, "SELECT root_name, status, span_count, error_count FROM traces"))[0]
    assert first == ("execute_tool lookup", "error", 1, 1)
    await send(client, project, [span("agent run", trace=t, sid=root, start=start, ms=400, attrs=LLM)])
    summary = (
        await rows(
            project.team.org_id, "SELECT root_name, status, span_count, round(duration_ms), models, tools FROM traces"
        )
    )[0]
    assert summary == ("agent run", "error", 2, 400, ["model-fast"], [])
    assert (await rows(project.team.org_id, "SELECT count(*) FROM traces"))[0][0] == 1


async def test_a_span_dated_far_in_the_past_is_kept_in_the_default_partition(client, project):
    old = datetime.now(UTC) - timedelta(days=60)
    await send(client, project, [span("late arrival", start=old)])
    stored = await rows(project.team.org_id, "SELECT name, tableoid::regclass::text FROM spans")
    assert stored == [("late arrival", "spans_default")]
    assert (await rows(project.team.org_id, "SELECT root_name FROM traces"))[0][0] == "late arrival"


async def test_a_plain_span_with_no_model_attributes_is_stored_as_it_is(client, project):
    await send(
        client,
        project,
        [span("GET /orders/:id", attrs={"http.request.method": "GET", "http.response.status_code": 200})],
    )
    row = (
        await rows(project.team.org_id, "SELECT name, operation, model, cost_usd, input_tokens, attributes FROM spans")
    )[0]
    assert row[:5] == ("GET /orders/:id", None, None, None, None)
    assert row[5] == {"http.request.method": "GET", "http.response.status_code": 200}
    assert (await rows(project.team.org_id, "SELECT cost_usd, models FROM traces"))[0] == (Decimal(0), [])


async def test_current_and_older_attribute_names_are_both_understood(client, project):
    old_style = {"gen_ai.system": "acme", "gen_ai.request.model": "m", "gen_ai.usage.prompt_tokens": 30,
                 "gen_ai.usage.completion_tokens": 7, "gen_ai.prompt": "hello", "gen_ai.completion": "hi"}  # fmt: skip
    resource = {"service.name": "shop", "service.version": "1.4.2"}
    attrs = old_style | {"session.id": "sess-9", "agentops.prompt.name": "greet", "agentops.prompt.version": 3}
    await send(client, project, [span(attrs=attrs, resource=resource)])
    r = (
        await rows(
            project.team.org_id,
            """SELECT provider, model, input_tokens, output_tokens, content, session_id,
             prompt_name, prompt_version, service_name, service_version, attributes ? 'gen_ai.prompt' FROM spans""",
        )
    )[0]
    assert r == ("acme", "m", 30, 7, {"input": "hello", "output": "hi"}, "sess-9", "greet", 3, "shop", "1.4.2", False)


async def test_secret_looking_values_are_masked_on_arrival(client, project):
    attrs = {"http.request.header.authorization": "Bearer abcdefghijklmnopqrstuvwxyz", "note": "key sk-abcdefghijklmnopqrstuv used",
             "gen_ai.prompt": "my key is sk-zyxwvutsrqponmlkjihgfedcba"}  # fmt: skip
    await send(client, project, [span(attrs=attrs, error="failed with token sk-0123456789abcdefghij")])
    stored = str(await rows(project.team.org_id, "SELECT attributes, content, status_message FROM spans"))
    assert "sk-" not in stored and "abcdefghijklmnopqrstuvwxyz" not in stored and "[redacted]" in stored


async def test_cost_uses_the_organisations_prices_and_is_unknown_without_one(client, project):
    org = project.team.org_id
    await rows(
        org,
        "INSERT INTO prices (org_id, model, input_per_mtok, output_per_mtok) VALUES (:o, 'model-fast', 2, 10) RETURNING model",
        o=org,
    )
    await send(client, project, [span(attrs=LLM), span(attrs=LLM | {"gen_ai.request.model": "unpriced"})])
    costs = dict(await rows(org, "SELECT model, cost_usd FROM spans"))
    assert costs == {"model-fast": Decimal("0.004"), "unpriced": None}  # 1000 x $2/M + 200 x $10/M
    assert (
        cost_for({}, "model-fast", 1, 1) is None and cost_for({"m": (Decimal(1), Decimal(1))}, "m", None, None) is None
    )


async def test_spans_are_private_to_the_organisation(client, project, other_team):
    await send(client, project, [span(attrs=LLM)])
    assert (await rows(other_team.org_id, "SELECT count(*) FROM spans"))[0][0] == 0
    assert (await rows(other_team.org_id, "SELECT count(*) FROM traces"))[0][0] == 0


async def test_partitions_cannot_be_read_around_the_parent_table(client, project):
    from sqlalchemy.exc import DBAPIError

    await send(client, project, [span()])
    part = (await rows(project.team.org_id, "SELECT tableoid::regclass::text FROM spans"))[0][0]
    assert part.startswith("spans_2")
    with pytest.raises(DBAPIError, match="permission denied"):
        await rows(project.team.org_id, f"SELECT count(*) FROM {part}")


async def test_two_batches_of_one_trace_written_at_the_same_time_give_the_right_summary(client, project):
    """Found by the final review: each transaction summarised the trace from its own snapshot, so the later one
    overwrote the summary without the other's spans (wrong root, span count 1, a failure hidden)."""
    import asyncio

    from app.db import tenant_session
    from app.ingest.otlp import parse_request
    from app.ingest.partitions import ensure_partitions
    from app.ingest.rollups import add_to_rollups
    from app.ingest.writer import normalise, write_spans
    from tests.otlp_helpers import make_batch

    await ensure_partitions(days_back=3, days_ahead=1)
    org = project.team.org_id
    t, root = trace_id(), span_id()
    start = datetime.now(UTC) - timedelta(minutes=5)

    def rows_for(spans):
        body, headers = make_batch(spans)
        return [normalise(s, org, project.id, {}) for s in parse_request(body, headers["Content-Type"]).spans]

    a = rows_for([span("agent run", trace=t, sid=root, start=start, ms=400)])
    b = rows_for([span("tool", trace=t, parent=root, start=start + timedelta(milliseconds=50), ms=100, error="boom")])

    async def worker_b():
        async with tenant_session(org) as db:
            await add_to_rollups(db, org, project.id, b, await write_spans(db, project.id, b))

    async with tenant_session(org) as db:  # worker A, still uncommitted while B runs
        await add_to_rollups(db, org, project.id, a, await write_spans(db, project.id, a))
        other = asyncio.create_task(worker_b())
        await asyncio.sleep(1.0)
    await other
    summary = (await rows(org, "SELECT root_name, status, span_count, error_count FROM traces"))[0]
    assert tuple(summary) == ("agent run", "error", 2, 1)


async def test_odd_values_are_cleaned_instead_of_losing_the_batch(client, project):
    """Found by the final review: one NUL character, a token count beyond 32 bits or a very long model name made
    PostgreSQL reject the whole batch, so up to 10,000 good spans went dead with it."""
    odd = span("tool\x00call", attrs={"gen_ai.completion": "abc\x00def", "gen_ai.request.model": "m" * 5000,
                                      "gen_ai.usage.input_tokens": 10**12, "note": "x\x00y"})  # fmt: skip
    await send(client, project, [span(), odd])
    org = project.team.org_id
    assert (await rows(org, "SELECT count(*) FROM spans"))[0][0] == 2
    assert (await rows(org, "SELECT count(*) FROM ingest_batches"))[0][0] == 0
    name, model, tokens, content, attrs = (
        await rows(
            org, "SELECT name, model, input_tokens, content::text, attributes::text FROM spans WHERE name LIKE 'tool%'"
        )
    )[0]
    assert name == "toolcall" and "\\u0000" not in content + attrs and "abcdef" in content
    assert len(model) <= 500 and tokens == 2**31 - 1
