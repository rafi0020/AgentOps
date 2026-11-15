"""Deterministic evaluators applied to a sample of live traffic as it is ingested."""

from datetime import UTC, datetime, timedelta

from app.monitors.online import sampled
from app.monitors.rules import evaluate_rules, last_complete_window
from tests.conftest import make_evaluator, rows, send
from tests.otlp_helpers import span, trace_id

NOW = datetime.now(UTC).replace(second=0, microsecond=0)
START, _ = last_complete_window(NOW, 60)


def answer(text, trace=None):
    return span(
        "chat model-fast",
        trace=trace,
        start=START + timedelta(minutes=10),
        attrs={"gen_ai.request.model": "m", "gen_ai.prompt": "q", "gen_ai.completion": text},
    )


async def test_sampling_is_deterministic_and_close_to_the_rate():
    ids = [trace_id() for _ in range(4000)]
    share = sum(sampled(t, 0.25) for t in ids) / len(ids)
    assert 0.22 < share < 0.28 and [sampled(t, 0.25) for t in ids[:50]] == [sampled(t, 0.25) for t in ids[:50]]
    assert all(sampled(t, 1.0) for t in ids[:100])


async def test_live_answers_are_scored_once_and_feed_a_score_monitor(client, project):
    team = project.team
    polite = await make_evaluator(project, "polite", "contains", {"text": "please"})
    assert (
        await team.engineer.put(
            "/api/monitors/online", json={"project_id": project.id, "evaluator_id": polite, "sample_rate": 1.0}
        )
    ).status_code == 200
    batch = (
        [answer("Sure, please wait.") for _ in range(5)]
        + [answer("No.") for _ in range(15)]
        + [span("GET /health", start=START + timedelta(minutes=10))]
    )
    await send(client, project, batch)
    await send(client, project, batch)  # the same batch again scores nothing twice
    scored = await rows(team.org_id, "SELECT count(*), sum(value) FROM trace_scores")
    assert scored[0] == (20, 5.0)  # the span without content is not scored
    rollup = await rows(team.org_id, "SELECT sum(count), sum(sum) FROM score_rollups_5m")
    assert rollup[0] == (20, 5.0)

    r = await team.engineer.post("/api/monitors/rules", json={"project_id": project.id, "name": "politeness", "metric": "score_avg", "evaluator_id": polite,
                                                              "comparison": "<", "threshold": 0.5, "window_minutes": 60})  # fmt: skip
    assert r.status_code == 201
    assert await evaluate_rules(team.org_id, NOW) == 1
    alert = (await team.viewer.get("/api/monitors/alerts")).json()["alerts"][0]
    assert alert["value"] == 0.25 and alert["calls"] == 20


async def test_a_judge_cannot_score_live_traffic(project):
    judge = await make_evaluator(project, "j", "judge", {"rubric": "The answer is correct and polite to the customer."})
    r = await project.team.engineer.put(
        "/api/monitors/online", json={"project_id": project.id, "evaluator_id": judge, "sample_rate": 0.5}
    )
    assert r.status_code == 422 and "deterministic" in r.json()["detail"]
    assert (
        await project.team.viewer.put(
            "/api/monitors/online", json={"project_id": project.id, "evaluator_id": judge, "sample_rate": 0.5}
        )
    ).status_code == 403


async def test_an_evaluator_that_breaks_never_stops_ingestion_and_can_be_switched_off(client, project):
    """Found by the final review: an online evaluator that raised failed the ingest transaction, so every batch
    with content went dead; and an online evaluator could not be removed."""
    team = project.team
    created = await team.engineer.post("/api/evaluators", json={"project_id": project.id, "name": "shape", "kind": "json_schema",
                                                                 "config": {"schema": {"type": "object"}}})  # fmt: skip
    evaluator = created.json()["id"]
    # Make it break the way a schema saved before validation existed would.
    await rows(
        team.org_id,
        'UPDATE evaluators SET config = \'{"schema": {"$ref": "http://127.0.0.1:9/x.json"}}\' WHERE id = :e RETURNING 1',
        e=evaluator,
    )
    put = await team.engineer.put(
        "/api/monitors/online", json={"project_id": project.id, "evaluator_id": evaluator, "sample_rate": 1}
    )
    assert put.status_code == 200
    await send(client, project, [span(attrs={"gen_ai.completion": '{"a": 1}'})])
    assert (await rows(team.org_id, "SELECT count(*) FROM spans"))[0][0] == 1
    assert (await rows(team.org_id, "SELECT count(*) FROM ingest_batches"))[0][0] == 0

    off = await team.engineer.delete(f"/api/monitors/online/{project.id}/{evaluator}")
    assert off.status_code == 204
    listed = (await team.engineer.get("/api/monitors/online", params={"project": project.id})).json()["online"]
    assert listed == []


async def test_a_resent_span_is_not_scored_again_when_a_new_span_shares_its_trace_and_start(
    client, project, monkeypatch
):
    """Found in the minor review: spans were matched to 'new' by (trace, start time), so a span that was already
    stored was evaluated again whenever a new span in the same trace started at the same moment. The store ignored
    the duplicate score, so the bug was wasted work, and the test counts evaluations."""
    from app.monitors import online

    calls = []
    real = online.evaluate

    def counting(*args, **kwargs):
        calls.append(kwargs["output"])
        return real(*args, **kwargs)

    monkeypatch.setattr(online, "evaluate", counting)
    team = project.team
    polite = await make_evaluator(project, "polite", "contains", {"text": "please"})
    await team.engineer.put(
        "/api/monitors/online", json={"project_id": project.id, "evaluator_id": polite, "sample_rate": 1.0}
    )
    trace, first_sid = trace_id(), "1111111111111111"
    first = span("chat model-fast", trace=trace, sid=first_sid, start=START + timedelta(minutes=10),
                 attrs={"gen_ai.request.model": "m", "gen_ai.prompt": "q", "gen_ai.completion": "Sure, please."})  # fmt: skip
    second = span("chat model-fast", trace=trace, start=START + timedelta(minutes=10),
                  attrs={"gen_ai.request.model": "m", "gen_ai.prompt": "q", "gen_ai.completion": "Yes, please."})  # fmt: skip
    await send(client, project, [first])
    await send(client, project, [first, second])  # the first span again, and a new one at the same instant
    assert (await rows(team.org_id, "SELECT count(*) FROM trace_scores"))[0][0] == 2
    assert calls == ["Sure, please.", "Yes, please."]  # the resent span was not evaluated again
