"""Roll-ups are exact, idempotent and mergeable."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db import tenant_session
from app.ingest.rollups import LATENCY_BOUNDS_MS, merge, percentile, query_rollups, slot_of
from tests.conftest import rows, send
from tests.otlp_helpers import span

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)  # on the hour


def llm(model="model-fast", ms=120, error=None, minutes=0, tokens=(100, 20), **extra):
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.request.model": model, "gen_ai.usage.input_tokens": tokens[0],
             "gen_ai.usage.output_tokens": tokens[1]} | extra  # fmt: skip
    return span(f"chat {model}", start=NOW + timedelta(minutes=minutes), ms=ms, error=error, attrs=attrs)


async def figures(project, **kwargs):
    async with tenant_session(project.team.org_id) as db:
        return await query_rollups(db, project.id, NOW - timedelta(hours=1), NOW + timedelta(hours=3), **kwargs)


def test_histogram_slots_and_percentiles():
    assert [slot_of(ms) for ms in (0, 10, 10.1, 99, 60000, 60001)] == [0, 0, 1, 3, 11, 12]
    hist = [0] * 13
    hist[3], hist[6], hist[12] = 90, 9, 1  # 90 calls up to 100 ms, 9 up to 1 s, 1 over a minute
    assert percentile(hist, 0.5) == 100.0 and percentile(hist, 0.95) == 1000.0 and percentile(hist, 0.999) == 120000.0
    assert percentile([0] * 13, 0.95) is None and percentile(None, 0.5) is None
    assert merge([hist, hist])[3] == 180 and len(LATENCY_BOUNDS_MS) == 12


async def test_rollups_equal_an_independent_count_of_the_spans(client, project):
    spans = [llm(ms=40), llm(ms=300, error="boom"), llm("model-large", ms=2000, tokens=(500, 80)),
             span("execute_tool lookup", start=NOW, ms=15, attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup"})]  # fmt: skip
    await send(client, project, spans)
    independent = (
        await rows(
            project.team.org_id,
            """SELECT count(*), count(*) FILTER (WHERE status = 'error'),
        sum(input_tokens), sum(output_tokens) FROM spans""",
        )
    )[0]
    total = (await figures(project))[0]
    assert (
        (total["calls"], total["errors"], total["input_tokens"], total["output_tokens"])
        == tuple(independent)
        == (4, 1, 700, 120)
    )
    assert total["error_rate"] == 0.25 and total["roots"] == 4
    by_model = {r["model"]: r for r in await figures(project, group_by=["model"])}
    assert (
        by_model["model-fast"]["calls"] == 2
        and by_model["model-large"]["p95_ms"] == 2500.0
        and by_model[""]["calls"] == 1
    )
    only_tool = await figures(project, filters={"tool": "lookup"})
    assert only_tool[0]["calls"] == 1 and only_tool[0]["p50_ms"] == 25.0


async def test_a_repeated_batch_adds_nothing(client, project):
    batch = [llm(), llm(error="x")]
    await send(client, project, batch)
    once = await figures(project, group_by=["model", "operation"])
    await send(client, project, batch)
    await send(client, project, batch, encoding="json")
    assert await figures(project, group_by=["model", "operation"]) == once
    assert once[0]["calls"] == 2 and once[0]["errors"] == 1


async def test_a_late_span_lands_in_the_bucket_it_belongs_to(client, project):
    await send(client, project, [llm(minutes=0), llm(minutes=30)])
    await send(client, project, [llm(minutes=1, ms=9000)])  # arrives afterwards, belongs to the first bucket
    series = await figures(project, step="5m")
    assert [(r["calls"], r["p95_ms"]) for r in series] == [(2, 10000.0), (1, 250.0)]


async def test_an_hour_is_the_sum_of_its_five_minute_buckets(client, project):
    await send(client, project, [llm(minutes=m, ms=50 + m) for m in range(0, 60, 5)] + [llm(minutes=65)])
    fine, hourly = await figures(project, step="5m"), await figures(project, step="1h")
    assert len(fine) == 13 and [r["calls"] for r in hourly] == [12, 1]
    assert sum(r["input_tokens"] for r in fine) == sum(r["input_tokens"] for r in hourly) == 1300


async def test_dimensions_for_prompt_and_service_version(client, project):
    v6 = {"agentops.prompt.name": "order-lookup", "agentops.prompt.version": 6}
    v7 = {"agentops.prompt.name": "order-lookup", "agentops.prompt.version": 7}
    await send(client, project, [llm(**v6), llm(**v6), llm(error="validation error", **v7)])
    by_version = {
        r["prompt_version"]: r["error_rate"] for r in await figures(project, group_by=["prompt_name", "prompt_version"])
    }
    assert by_version == {6: 0.0, 7: 1.0}


async def test_rollups_are_private_and_unknown_group_names_are_ignored(client, project, other_team):
    await send(client, project, [llm()])
    async with tenant_session(other_team.org_id) as db:
        assert await query_rollups(db, None, NOW - timedelta(days=1), NOW + timedelta(days=1)) == [
            pytest.approx({"calls": 0, "errors": 0, "error_rate": None, "roots": 0, "input_tokens": 0, "output_tokens": 0,
                           "cost_usd": 0.0, "p50_ms": None, "p95_ms": None})
        ]  # fmt: skip
    assert (await figures(project, group_by=["model; DROP", "status"]))[0]["calls"] == 1


async def test_histograms_read_back_equal_the_stored_rows_merged(lumen):
    """Guards the slot-by-slot summing in query_rollups (found by the scale test: the array aggregate it
    replaced took 0.9 s for 30 days of one organisation's roll-ups)."""
    from tests.conftest import DEMO_END

    start = DEMO_END - timedelta(days=30)
    async with tenant_session(lumen.org_id) as db:
        stored = (
            await db.execute(
                text("SELECT model, latency_hist FROM rollups_5m WHERE bucket >= :s AND bucket < :e"),
                {"s": start, "e": DEMO_END},
            )
        ).all()
        whole = (await query_rollups(db, None, start, DEMO_END))[0]
        by_model = {r["model"]: r for r in await query_rollups(db, None, start, DEMO_END, group_by=["model"])}
    assert len(stored) > 1000
    expected = merge([r.latency_hist for r in stored])
    assert (whole["p50_ms"], whole["p95_ms"]) == (percentile(expected, 0.5), percentile(expected, 0.95))
    assert whole["calls"] == sum(expected)
    for model in {r.model for r in stored}:
        mine = merge([r.latency_hist for r in stored if r.model == model])
        assert (by_model[model]["p50_ms"], by_model[model]["p95_ms"]) == (percentile(mine, 0.5), percentile(mine, 0.95))


async def test_an_organisation_wide_read_does_not_scan_other_organisations_rollups(project):
    """Found by the scale test: the only index led with project_id, so a read across all of one organisation's
    projects (the Overview with no project chosen) had to read every organisation's roll-ups."""
    async with tenant_session(project.team.org_id) as db:
        await db.execute(text("SET LOCAL enable_seqscan = off"))  # with tiny test tables the planner prefers a scan
        for table, column in (("rollups_5m", "calls"), ("score_rollups_5m", "count")):
            sql = f"SELECT sum({column}) FROM {table} WHERE bucket >= now() - interval '1 day' AND bucket < now()"
            plan = (await db.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"))).scalar()
            conditions: list[str] = []

            def walk(node, table=table, conditions=conditions):
                if node.get("Relation Name") == table:
                    conditions.append(node.get("Index Cond", ""))
                for child in node.get("Plans", []):
                    walk(child)

            walk(plan[0]["Plan"])
            assert any("bucket" in c and "org_id" in c for c in conditions), (table, conditions)
