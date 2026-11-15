"""Trace list, trace detail, sessions and chart figures over HTTP."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import send
from tests.otlp_helpers import span, span_id, trace_id

NOW = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=2)
CHAT = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "model-fast", "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 20, "gen_ai.prompt": "the customer asked about order 4417", "gen_ai.completion": "it shipped"}  # fmt: skip


async def seed(client, project) -> dict:
    """Three traces: an agent run with a failing tool call (in a session), a slow one, and a plain one."""
    t1, root = trace_id(), span_id()
    await send(client, project, [
        span("agent run", trace=t1, sid=root, start=NOW, ms=900, attrs={"session.id": "sess-1"}, resource={"service.name": "shop", "service.version": "2.0"}),
        span("chat model-fast", trace=t1, parent=root, start=NOW + timedelta(milliseconds=10), ms=300, attrs=CHAT),
        span("execute_tool lookup_order", trace=t1, parent=root, start=NOW + timedelta(milliseconds=400), ms=80,
             error="validation error: order_id", attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order"}),
    ])  # fmt: skip
    t2 = trace_id()
    await send(client, project, [span("chat model-large", trace=t2, start=NOW + timedelta(minutes=10), ms=7000,
                                      attrs=CHAT | {"gen_ai.request.model": "model-large", "session.id": "sess-1"})])  # fmt: skip
    t3 = trace_id()
    await send(client, project, [span("GET /health", trace=t3, start=NOW + timedelta(minutes=20), ms=5)])
    return {"failing": t1, "slow": t2, "plain": t3}


def window(**extra) -> dict:
    return {"from": (NOW - timedelta(hours=1)).isoformat(), "to": (NOW + timedelta(hours=1)).isoformat()} | extra


async def names(client, project, **filters) -> list[str]:
    r = await client.get("/api/traces", params=window(project=project.id, **filters))
    assert r.status_code == 200, r.text
    return [t["root_name"] for t in r.json()["traces"]]


async def test_list_is_newest_first_and_each_filter_narrows_it(client, project):
    team = project.team
    await seed(client, project)
    assert await names(team.viewer, project) == ["GET /health", "chat model-large", "agent run"]
    assert await names(team.viewer, project, status="error") == ["agent run"]
    assert await names(team.viewer, project, model="model-large") == ["chat model-large"]
    assert await names(team.viewer, project, tool="lookup_order") == ["agent run"]
    assert await names(team.viewer, project, service_version="2.0") == ["agent run"]
    assert await names(team.viewer, project, session="sess-1") == ["chat model-large", "agent run"]
    assert await names(team.viewer, project, min_duration_ms=1000) == ["chat model-large"]
    assert await names(team.viewer, project, q="health") == ["GET /health"]
    assert await names(team.viewer, project, q="100%_") == []


async def test_pages_never_overlap_or_skip(client, project):
    team = project.team
    await send(client, project, [span(f"t{i:02d}", start=NOW + timedelta(seconds=i)) for i in range(25)])
    seen, cursor = [], None
    while True:
        r = (
            await team.viewer.get(
                "/api/traces", params=window(project=project.id, limit=10) | ({"cursor": cursor} if cursor else {})
            )
        ).json()
        seen += [t["root_name"] for t in r["traces"]]
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert seen == [f"t{i:02d}" for i in reversed(range(25))]
    assert (
        await team.viewer.get("/api/traces", params=window(project=project.id, cursor="garbage"))
    ).status_code == 422


async def test_trace_detail_orders_and_nests_spans(client, project):
    ids = await seed(client, project)
    body = (await project.team.engineer.get(f"/api/traces/{project.id}/{ids['failing']}")).json()
    assert body["status"] == "error" and body["span_count"] == 3 and body["tools"] == ["lookup_order"]
    assert [(s["name"], s["depth"], s["offset_ms"]) for s in body["spans"]] == [
        ("agent run", 0, 0.0), ("chat model-fast", 1, 10.0), ("execute_tool lookup_order", 1, 400.0),
    ]  # fmt: skip
    assert body["spans"][2]["status_message"] == "validation error: order_id"
    assert body["spans"][1]["content"] == {"input": "the customer asked about order 4417", "output": "it shipped"}


async def test_viewers_see_structure_but_not_prompt_content(client, project):
    ids = await seed(client, project)
    spans = (await project.team.viewer.get(f"/api/traces/{project.id}/{ids['failing']}")).json()["spans"]
    assert spans[1]["content"] is None and spans[1]["has_content"] is True and spans[1]["model"] == "model-fast"


async def test_another_organisation_cannot_see_traces_projects_or_figures(client, project, other_team):
    ids = await seed(client, project)
    for path in (f"/api/traces/{project.id}/{ids['failing']}", f"/api/sessions/{project.id}/sess-1"):
        assert (await other_team.admin.get(path)).status_code == 404
    for path in ("/api/traces", "/api/sessions", "/api/metrics/overview", "/api/metrics/series"):
        assert (await other_team.admin.get(path, params=window(project=project.id))).status_code == 404
    everything = (await other_team.admin.get("/api/metrics/overview", params=window())).json()
    assert everything["current"]["calls"] == 0 and everything["by_project"] == []
    assert (await client.get("/api/traces", params=window(project=project.id))).status_code == 401


async def test_sessions_group_traces(client, project):
    await seed(client, project)
    team = project.team
    sessions = (await team.viewer.get("/api/sessions", params=window(project=project.id))).json()["sessions"]
    assert (
        len(sessions) == 1
        and sessions[0]["session_id"] == "sess-1"
        and sessions[0]["traces"] == 2
        and sessions[0]["errors"] == 1
    )
    detail = (await team.viewer.get(f"/api/sessions/{project.id}/sess-1")).json()
    assert [t["root_name"] for t in detail["traces"]] == ["agent run", "chat model-large"]


@pytest.mark.parametrize(
    "params",
    [{"from": "2026-01-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"}, {"from": "2026-03-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
     {"from": "2026-01-01T00:00:00"}],
)  # fmt: skip
async def test_ranges_are_bounded_and_need_a_timezone(project, params):
    for path in ("/api/traces", "/api/metrics/overview"):
        assert (await project.team.viewer.get(path, params=params | {"project": project.id})).status_code == 422


async def test_overview_and_series_match_what_was_sent(client, project):
    await seed(client, project)
    team = project.team
    overview = (await team.viewer.get("/api/metrics/overview", params=window(project=project.id))).json()
    assert (
        overview["current"]["calls"] == 5 and overview["current"]["errors"] == 1 and overview["current"]["roots"] == 3
    )
    assert overview["model_calls"]["calls"] == 2 and overview["model_calls"]["p95_ms"] == 10000.0
    assert overview["previous"]["calls"] == 0 and overview["by_project"][0]["project_id"] == project.id
    by_tool = (
        await team.viewer.get(
            "/api/metrics/series", params=window(project=project.id, group_by="tool", operation="execute_tool")
        )
    ).json()
    assert by_tool["rows"] == [pytest.approx(by_tool["rows"][0])] and by_tool["rows"][0]["tool"] == "lookup_order"
    assert by_tool["rows"][0]["error_rate"] == 1.0
    hourly = (await team.viewer.get("/api/metrics/series", params=window(project=project.id, step="1h"))).json()
    assert sum(r["calls"] for r in hourly["rows"]) == 5
