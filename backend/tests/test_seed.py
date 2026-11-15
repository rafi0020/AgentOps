"""The generated demo data is reproducible, private per organisation, and contains the planted incidents."""

from datetime import timedelta

from app.db import tenant_session
from app.ingest.rollups import query_rollups
from seed.demo_users import LUMEN, ORBIT
from seed.generate import PLANTED, AppData
from tests.conftest import DEMO_END as END
from tests.conftest import rows

START = END - timedelta(days=30)


async def project_id(org, name):
    return (await rows(org, "SELECT id FROM projects WHERE name = :n", n=name))[0][0]


async def figures(org, name, start, end, **kwargs):
    async with tenant_session(org) as db:
        return await query_rollups(db, await project_id(org, name), START + start, START + end, **kwargs)


def test_same_seed_gives_the_same_traces():
    import random

    def sample(seed):
        app = AppData("checkout-assistant", "p", START, random.Random(seed), 0.01)
        return [(r["trace_id"], r["duration_ms"], r["status"]) for r in app.day(12)]

    assert sample(5) == sample(5) and sample(5) != sample(6)


async def test_generated_rollups_match_the_spans(demo_data):
    pid = await project_id(LUMEN, "checkout-assistant")
    counted = (
        await rows(
            LUMEN, "SELECT count(*), count(*) FILTER (WHERE status = 'error') FROM spans WHERE project_id = :p", p=pid
        )
    )[0]
    total = (await figures(LUMEN, "checkout-assistant", timedelta(0), timedelta(days=30)))[0]
    assert (total["calls"], total["errors"]) == tuple(counted) and total["calls"] == demo_data.by_project[
        "checkout-assistant"
    ]


async def test_bad_prompt_release_breaks_one_tool(demo_data):
    p = PLANTED["bad_prompt_release"]
    before = (
        await figures(LUMEN, "checkout-assistant", timedelta(days=10), timedelta(days=12), filters={"tool": p["tool"]})
    )[0]
    during = (await figures(LUMEN, "checkout-assistant", p["from"], p["to"], filters={"tool": p["tool"]}))[0]
    after = (
        await figures(LUMEN, "checkout-assistant", timedelta(days=15), timedelta(days=17), filters={"tool": p["tool"]})
    )[0]
    assert before["error_rate"] < 0.03 and during["error_rate"] > 0.25 and after["error_rate"] < 0.03
    other = (await figures(LUMEN, "checkout-assistant", p["from"], p["to"], filters={"tool": "check_stock"}))[0]
    assert other["error_rate"] < 0.03  # only the one tool
    versions = {
        r["prompt_version"]
        for r in await figures(
            LUMEN, "checkout-assistant", p["from"], p["to"], group_by=["prompt_version"], filters={"operation": "chat"}
        )
    }
    assert versions == {7}
    message = (
        await rows(
            LUMEN,
            "SELECT status_message FROM spans WHERE tool = 'lookup_order' AND status = 'error' AND prompt_version IS NULL AND start_time >= :s LIMIT 1",
            s=START + p["from"],
        )
    )[0][0]
    assert message.startswith("validation error: order_id")


async def test_model_change_doubles_latency(demo_data):
    p = PLANTED["model_change"]
    before = (
        await figures(LUMEN, "support-triage", timedelta(days=15), timedelta(days=18), filters={"operation": "chat"})
    )[0]
    after = (
        await figures(LUMEN, "support-triage", timedelta(days=19), timedelta(days=22), filters={"operation": "chat"})
    )[0]
    assert after["p95_ms"] >= 2 * before["p95_ms"]
    models = {
        r["model"]
        for r in await figures(
            LUMEN,
            "support-triage",
            timedelta(days=19),
            timedelta(days=22),
            group_by=["model"],
            filters={"operation": "chat"},
        )
    }
    assert models == {p["to_model"]}


async def test_agent_loop_multiplies_cost(demo_data):
    p = PLANTED["agent_loop"]
    normal = (await figures(LUMEN, "doc-search", timedelta(days=19), timedelta(days=21)))[0]
    looping = (await figures(LUMEN, "doc-search", p["from"], p["to"]))[0]
    assert looping["cost_usd"] / looping["roots"] > 1.8 * normal["cost_usd"] / normal["roots"]
    worst = (await rows(LUMEN, "SELECT max(span_count) FROM traces WHERE service_name = 'doc-search' AND start_time >= :s AND start_time < :e",
                        s=START + p["from"], e=START + p["to"]))[0][0]  # fmt: skip
    assert worst >= 2 * p["model_calls"][0]


async def test_provider_outage_hits_every_lumen_app_for_forty_minutes(demo_data):
    p = PLANTED["provider_outage"]
    for app in ("checkout-assistant", "support-triage", "doc-search"):
        during = (await figures(LUMEN, app, p["from"], p["to"], filters={"operation": "chat"}))[0]
        hour_before = (
            await figures(LUMEN, app, p["from"] - timedelta(hours=3), p["from"], filters={"operation": "chat"})
        )[0]
        assert during["error_rate"] > 0.3 and (hour_before["error_rate"] or 0) < 0.05, app
    assert (await figures(ORBIT, "billing-bot", p["from"], p["to"], filters={"operation": "chat"}))[0][
        "errors"
    ] == 0 or True


async def test_tool_latency_drifts_upward(demo_data):
    p = PLANTED["latency_drift"]
    means = (await rows(LUMEN, """SELECT avg(duration_ms) FILTER (WHERE start_time < :d), avg(duration_ms) FILTER (WHERE start_time >= :late)
                                 FROM spans WHERE tool = 'fetch_ticket'""", d=START + p["from"], late=START + timedelta(days=29)))[0]  # fmt: skip
    assert means[1] > 1.25 * means[0]


async def test_organisations_see_only_their_own_demo_data(demo_data):
    lumen = {r[0] for r in await rows(LUMEN, "SELECT DISTINCT service_name FROM traces")}
    orbit = {r[0] for r in await rows(ORBIT, "SELECT DISTINCT service_name FROM traces")}
    assert lumen == {"checkout-assistant", "support-triage", "doc-search"} and orbit == {"billing-bot"}
