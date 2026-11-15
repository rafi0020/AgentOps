"""Monitors fire once per window on real breaches, ignore thin windows, and can start an investigation."""

from datetime import UTC, datetime, timedelta

from app.monitors.rules import evaluate_rules, last_complete_window
from tests.conftest import send
from tests.otlp_helpers import span

NOW = datetime.now(UTC).replace(second=0, microsecond=0)
WINDOW_START, WINDOW_END = last_complete_window(NOW, 15)


def tool(error=None, at=None, ms=80):
    return span("execute_tool lookup_order", start=at or WINDOW_START + timedelta(minutes=3), ms=ms, error=error,
                attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order"})  # fmt: skip


async def rule(project, **overrides):
    body = {"project_id": project.id, "name": "lookup errors", "metric": "error_rate", "comparison": ">", "threshold": 0.2,
            "window_minutes": 15, "filter": {"tool": "lookup_order"}} | overrides  # fmt: skip
    r = await project.team.engineer.post("/api/monitors/rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_the_window_ends_at_the_last_finished_bucket():
    start, end = last_complete_window(datetime(2026, 11, 14, 9, 13, 40, tzinfo=UTC), 15)
    assert (start, end) == (datetime(2026, 11, 14, 8, 45, tzinfo=UTC), datetime(2026, 11, 14, 9, 0, tzinfo=UTC))


async def test_a_breach_raises_one_alert_for_its_window(client, project):
    team = project.team
    r = await rule(project)
    assert r["description"] == "error rate (tool lookup_order) above 0.2 over 15 min"
    await send(client, project, [tool("validation error") for _ in range(10)] + [tool() for _ in range(20)])
    assert await evaluate_rules(team.org_id, NOW) == 1
    assert await evaluate_rules(team.org_id, NOW) == 0  # the same window again
    alerts = (await team.viewer.get("/api/monitors/alerts")).json()
    assert alerts["unacknowledged"] == 1 and alerts["alerts"][0]["calls"] == 30
    assert (
        abs(alerts["alerts"][0]["value"] - 1 / 3) < 1e-6
    )  # rates are rounded to six places and alerts["alerts"][0]["project"] == "checkout-assistant"


async def test_a_window_with_too_few_calls_raises_nothing(client, project):
    await rule(project)
    await send(client, project, [tool("boom"), tool("boom"), tool()])  # 67% errors, but only 3 calls
    assert await evaluate_rules(project.team.org_id, NOW) == 0


async def test_latency_cost_and_paused_rules(client, project):
    team = project.team
    await rule(project, name="slow", metric="p95_latency_ms", threshold=1000, filter={}, min_calls=5)
    paused = await rule(project, name="paused", min_calls=1, filter={})
    await team.engineer.patch(f"/api/monitors/rules/{paused['id']}", params={"status": "paused"})
    await send(client, project, [tool(ms=3000, error="x") for _ in range(6)])
    assert await evaluate_rules(team.org_id, NOW) == 1
    assert [a["rule"]["name"] for a in (await team.viewer.get("/api/monitors/alerts")).json()["alerts"]] == ["slow"]


async def test_acknowledging_and_investigating_an_alert(client, project, stub_llm):
    team = project.team
    await rule(project)
    await send(client, project, [tool("validation error") for _ in range(25)])
    await evaluate_rules(team.org_id, NOW)
    alert = (await team.viewer.get("/api/monitors/alerts")).json()["alerts"][0]
    assert (await team.viewer.post(f"/api/monitors/alerts/{alert['id']}/ack")).status_code == 403
    started = await team.engineer.post(f"/api/monitors/alerts/{alert['id']}/investigate")
    assert started.status_code == 202
    again = await team.engineer.post(f"/api/monitors/alerts/{alert['id']}/investigate")
    assert again.json() == started.json()  # not a second investigation
    run = (await team.viewer.get(f"/api/investigations/{started.json()['id']}")).json()
    assert (
        "lookup errors" in run["question"] and "checkout-assistant" in run["question"] and "25 calls" in run["question"]
    )
    assert (await team.engineer.post(f"/api/monitors/alerts/{alert['id']}/ack")).json() == {"acknowledged": True}
    assert (await team.viewer.get("/api/monitors/alerts", params={"open_only": True})).json()["alerts"] == []


async def test_rules_are_validated_and_private(project, other_team):
    team = project.team
    assert (
        await team.viewer.post(
            "/api/monitors/rules",
            json={"project_id": project.id, "name": "x", "metric": "error_rate", "comparison": ">", "threshold": 1},
        )
    ).status_code == 403
    bad = {"project_id": project.id, "name": "x", "metric": "score_avg", "comparison": "<", "threshold": 0.5}
    assert (
        await team.engineer.post("/api/monitors/rules", json=bad)
    ).status_code == 422  # a score monitor needs an evaluator
    assert (
        await team.engineer.post("/api/monitors/rules", json=bad | {"metric": "error_rate", "window_minutes": 7})
    ).status_code == 422
    r = await rule(project)
    assert (
        await other_team.engineer.post(
            "/api/monitors/rules",
            json={"project_id": project.id, "name": "x", "metric": "error_rate", "comparison": ">", "threshold": 1},
        )
    ).status_code == 404
    assert (await other_team.admin.get("/api/monitors/rules")).json()["rules"] == []
    assert (
        await other_team.engineer.patch(f"/api/monitors/rules/{r['id']}", params={"status": "paused"})
    ).status_code == 404


def floor_to(moment: datetime, minutes: int) -> datetime:
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % (minutes * 60), UTC)


async def test_a_longer_window_alerts_once_however_often_it_is_checked(client, project):
    """A 15-minute window is checked every 5 minutes; the same window must not raise a second alert."""
    team = project.team
    await rule(project, window_minutes=15)
    end = floor_to(NOW - timedelta(minutes=30), 15)  # a complete window that ended half an hour ago
    inside = end - timedelta(minutes=2)  # inside the window and inside any sliding window checked later
    await send(
        client, project, [tool("validation error", at=inside) for _ in range(10)] + [tool(at=inside) for _ in range(20)]
    )
    first = end + timedelta(minutes=6)
    assert await evaluate_rules(team.org_id, first) == 1
    assert await evaluate_rules(team.org_id, first + timedelta(minutes=5)) == 0
    assert await evaluate_rules(team.org_id, first + timedelta(minutes=8)) == 0  # still the same window
    assert (await team.viewer.get("/api/monitors/alerts")).json()["unacknowledged"] == 1


def test_windows_are_fixed_intervals_not_sliding_ones():
    now = datetime(2026, 11, 14, 9, 13, 40, tzinfo=UTC)
    assert last_complete_window(now, 15) == (
        datetime(2026, 11, 14, 8, 45, tzinfo=UTC),
        datetime(2026, 11, 14, 9, 0, tzinfo=UTC),
    )
    assert last_complete_window(now, 5) == (
        datetime(2026, 11, 14, 9, 5, tzinfo=UTC),
        datetime(2026, 11, 14, 9, 10, tzinfo=UTC),
    )
    assert last_complete_window(datetime(2026, 11, 14, 9, 14, 59, tzinfo=UTC), 15) == last_complete_window(now, 15)


async def test_two_simultaneous_investigate_clicks_start_one_run(client, project, stub_llm):
    import asyncio

    team = project.team
    await rule(project)
    await send(client, project, [tool("validation error") for _ in range(25)])
    await evaluate_rules(team.org_id, NOW)
    alert = (await team.viewer.get("/api/monitors/alerts")).json()["alerts"][0]
    first, second = await asyncio.gather(
        team.engineer.post(f"/api/monitors/alerts/{alert['id']}/investigate"),
        team.engineer.post(f"/api/monitors/alerts/{alert['id']}/investigate"),
    )
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    runs = (await team.admin.get("/api/investigations", params={"limit": 100})).json()["runs"]
    assert sum("lookup errors" in r["question"] for r in runs) == 1
