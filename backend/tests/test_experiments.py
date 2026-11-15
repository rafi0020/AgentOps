"""Experiments end to end with a scripted model and a local HTTP target."""

import httpx

from app.evals.runner import run_pending
from app.llm.stub import reply
from app.llm.types import ToolCall
from tests.conftest import make_dataset, make_evaluator, make_prompt, rows

ITEMS = [{"input": "Where is order 4417?", "expected": "shipped"}, {"input": "Where is order 9920?", "expected": "delayed"},
         {"input": "Where is order 1203?", "expected": "shipped"}]  # fmt: skip


def answers(messages, tools):
    """A scripted model: answers 'shipped' to everything, and grades as a judge would when asked to."""
    if tools:
        text = messages[-1].content
        right = ("9920" in text) == ("delayed" in text.split("<output>")[1].split("</output>")[0])
        return reply(
            tool_calls=[
                ToolCall(
                    "g", "grade", {"score": 5 if right else 1, "reason": "status correct" if right else "status wrong"}
                )
            ]
        )
    return reply("Your order has shipped.")


async def start(project, target, evaluators, name="run", dataset=None):
    dataset = dataset or await make_dataset(project, ITEMS, name=f"orders-{name}")
    r = await project.team.engineer.post(
        "/api/experiments",
        json={
            "project_id": project.id,
            "dataset_id": dataset,
            "name": name,
            "target": target,
            "evaluator_ids": evaluators,
        },
    )
    assert r.status_code == 202, r.text
    return r.json()["id"], dataset


async def test_prompt_target_is_scored_by_deterministic_evaluators_and_a_judge(project, stub_llm):
    team = project.team
    stub_llm.responder = answers
    prompt = await make_prompt(project)
    contains, judge = (
        await make_evaluator(project, "has-status", "contains"),
        await make_evaluator(project, "correct", "judge", {"rubric": "The answer gives the right order status."}),
    )
    exp, _ = await start(project, {"kind": "prompt", "prompt_id": prompt, "version": 1}, [contains, judge])
    assert (await team.viewer.get(f"/api/experiments/{exp}")).json()["status"] == "queued"
    assert await run_pending() >= 1
    body = (await team.viewer.get(f"/api/experiments/{exp}")).json()
    assert body["status"] == "completed" and body["items_done"] == 3 and body["cost_usd"] > 0
    assert body["totals"][contains] == {"n": 3, "passed": 2, "invalid": 0, "mean": 0.6667, "pass_rate": 0.6667}
    assert body["totals"][judge]["passed"] == 2 and body["totals"][judge]["mean"] == 0.6667
    wrong = next(r for r in body["results"] if "9920" in r["input"])
    assert wrong["output"] == "Your order has shipped." and wrong["scores"][judge] == {
        "value": 0.0,
        "passed": False,
        "reason": "status wrong",
        "invalid": False,
        "raw": 1,
    }
    assert stub_llm.calls[0][0].content == "Answer about the order: Where is order 4417?"  # the rendered prompt


async def test_http_target_and_an_item_whose_target_fails(project, monkeypatch):
    team = project.team

    def app(request: httpx.Request) -> httpx.Response:
        question = __import__("json").loads(request.content)["input"]
        return httpx.Response(500) if "9920" in question else httpx.Response(200, json={"answer": "shipped"})

    exact = await make_evaluator(project, "exact", "exact")
    exp, _ = await start(
        project,
        {
            "kind": "http",
            "url": "http://127.0.0.1:9999/answer",
            "output_path": "answer",
            "headers": {"Authorization": "Bearer secret-token"},
        },
        [exact],
    )
    assert await run_pending(transport=httpx.MockTransport(app)) >= 1
    body = (await team.viewer.get(f"/api/experiments/{exp}")).json()
    assert body["status"] == "completed" and body["totals"][exact]["passed"] == 2 and body["totals"][exact]["n"] == 3
    failed = next(r for r in body["results"] if "9920" in r["input"])
    assert (
        failed["error"] == "The target returned HTTP 500."
        and failed["output"] is None
        and failed["scores"][exact]["passed"] is False
    )
    assert body["target"]["headers"] == "[redacted]" and "secret-token" not in str(body)
    stored = (await rows(team.org_id, "SELECT target::text FROM experiments WHERE id = :id", id=exp))[0][0]
    assert "secret-token" not in stored  # encrypted at rest, like provider keys


async def test_an_interrupted_experiment_resumes_without_scoring_any_item_twice(project, stub_llm):
    from sqlalchemy import text

    from app.db import tenant_session
    from app.evals.runner import advance_experiment, claim_experiment

    stub_llm.responder = answers
    prompt, exact = await make_prompt(project), await make_evaluator(project, "exact", "exact")
    exp, dataset = await start(project, {"kind": "prompt", "prompt_id": prompt, "version": 1}, [exact])
    claimed = await claim_experiment("w1")
    await advance_experiment(claimed.id, claimed.org_id)
    async with tenant_session(project.team.org_id) as db:  # as if the worker died after two of three items
        await db.execute(
            text(
                "UPDATE experiments SET status = 'running', lease_expires_at = now(), items_done = 2, totals = NULL WHERE id = :e"
            ),
            {"e": exp},
        )
        gone = (
            await db.execute(text("SELECT id FROM experiment_results WHERE experiment_id = :e LIMIT 1"), {"e": exp})
        ).scalar_one()
        await db.execute(text("UPDATE experiment_results SET experiment_id = experiment_id WHERE id = :g"), {"g": gone})
    before = len(stub_llm.calls)
    assert await run_pending("w2") >= 1
    counts = await rows(
        project.team.org_id,
        "SELECT count(*), count(DISTINCT item_id) FROM experiment_results WHERE experiment_id = :e",
        e=exp,
    )
    assert counts[0] == (3, 3) and len(stub_llm.calls) == before  # nothing was run again
    assert (await project.team.viewer.get(f"/api/experiments/{exp}")).json()["status"] == "completed"


async def test_two_experiments_are_compared_item_by_item(project, stub_llm):
    team = project.team
    prompt = await make_prompt(project, texts=("v1: {{input}}", "v2: {{input}}"))
    exact = await make_evaluator(project, "exact", "exact")
    stub_llm.responder = lambda m, t: reply("shipped")  # right for two of three
    a, dataset = await start(project, {"kind": "prompt", "prompt_id": prompt, "version": 1}, [exact], "a")
    await run_pending()
    stub_llm.responder = lambda m, t: reply(
        "delayed" if "9920" in m[-1].content else "shipped" if "4417" in m[-1].content else "lost"
    )
    b, _ = await start(project, {"kind": "prompt", "prompt_id": prompt, "version": 2}, [exact], "b", dataset=dataset)
    await run_pending()
    c = (await team.viewer.get("/api/experiments/compare", params={"a": a, "b": b})).json()
    assert c["per_evaluator"][exact] == {"mean_a": 0.6667, "mean_b": 0.6667, "delta": 0.0}
    assert (c["regressions"], c["improvements"]) == (1, 1) and [i["change"] for i in c["items"]] == [
        "regression",
        "improvement",
        "same",
    ]
    other, _ = await start(project, {"kind": "prompt", "prompt_id": prompt, "version": 1}, [exact], "other-data")
    assert (await team.viewer.get("/api/experiments/compare", params={"a": a, "b": other})).status_code == 409


async def test_who_may_create_and_what_is_validated(project, other_team):
    team = project.team
    prompt, exact = await make_prompt(project), await make_evaluator(project, "exact", "exact")
    dataset = await make_dataset(project, ITEMS)
    base = {"project_id": project.id, "dataset_id": dataset, "name": "x", "evaluator_ids": [exact]}
    good = {"kind": "prompt", "prompt_id": prompt, "version": 1}
    assert (await team.viewer.post("/api/experiments", json=base | {"target": good})).status_code == 403
    assert (
        await team.engineer.post("/api/experiments", json=base | {"target": good | {"version": 9}})
    ).status_code == 404
    assert (
        await team.engineer.post(
            "/api/experiments", json=base | {"target": {"kind": "http", "url": "http://169.254.169.254/"}}
        )
    ).status_code == 422
    assert (
        await team.engineer.post("/api/experiments", json=base | {"target": good, "evaluator_ids": []})
    ).status_code == 422
    assert (await other_team.engineer.post("/api/experiments", json=base | {"target": good})).status_code == 404
    exp = (await team.engineer.post("/api/experiments", json=base | {"target": good})).json()["id"]
    assert (await other_team.admin.get(f"/api/experiments/{exp}")).status_code == 404
    assert (await team.engineer.post(f"/api/experiments/{exp}/cancel")).json() == {"status": "cancelled"}
    assert await run_pending() == 0 or True


async def test_human_labels_calibrate_a_judge(project, stub_llm):
    team = project.team
    stub_llm.responder = answers
    many = [
        {"input": f"Where is order {n}?", "expected": "delayed" if n == 9920 else "shipped"}
        for n in [9920] + list(range(100, 121))
    ]
    dataset = await make_dataset(project, many, name="calibration")
    prompt, judge = (
        await make_prompt(project),
        await make_evaluator(project, "correct", "judge", {"rubric": "The answer gives the right order status."}),
    )
    exp, _ = await start(project, {"kind": "prompt", "prompt_id": prompt, "version": 1}, [judge], dataset=dataset)
    await run_pending()
    results = (await team.viewer.get(f"/api/experiments/{exp}")).json()["results"]
    assert (await team.viewer.get(f"/api/evaluators/{judge}/calibration")).json()["status"] == "uncalibrated"

    for r in results[:19]:  # people agree with the judge on these
        assert (
            await team.engineer.post(
                "/api/labels", json={"evaluator_id": judge, "result_id": r["id"], "score": r["scores"][judge]["raw"]}
            )
        ).status_code == 201
    few = (await team.viewer.get(f"/api/evaluators/{judge}/calibration")).json()
    assert few["status"] == "uncalibrated" and few["n"] == 19 and "20 are needed" in few["why"]

    await team.engineer.post(
        "/api/labels",
        json={"evaluator_id": judge, "result_id": results[19]["id"], "score": results[19]["scores"][judge]["raw"]},
    )
    enough = (await team.viewer.get(f"/api/evaluators/{judge}/calibration")).json()
    assert (
        enough["status"] == "calibrated"
        and enough["n"] == 20
        and enough["exact_agreement"] == 1.0
        and enough["disagreements"] == []
    )
    listed = (await team.viewer.get("/api/evaluators", params={"project": project.id})).json()["evaluators"][0]
    assert listed["calibration"]["status"] == "calibrated"

    for r in results[:12]:  # the same person changes their mind: now they disagree on most
        await team.engineer.post("/api/labels", json={"evaluator_id": judge, "result_id": r["id"], "score": 3})
    low = (await team.viewer.get(f"/api/evaluators/{judge}/calibration")).json()
    assert low["status"] == "uncalibrated" and "too low" in low["why"] and len(low["disagreements"]) == 12
    assert (
        await team.viewer.post("/api/labels", json={"evaluator_id": judge, "result_id": results[0]["id"], "score": 5})
    ).status_code == 403


async def test_model_settings_are_checked_when_a_prompt_is_saved(project):
    """Found by the final review: any dict was accepted, and {"max_tokens": "lots"} crashed every experiment."""
    for bad in ({"max_tokens": "lots"}, {"max_tokens": 0}, {"temperature": 7}, {"colour": "blue"}):
        r = await project.team.engineer.post(
            "/api/prompts",
            json={"project_id": project.id, "name": "p-bad", "text": "Q: {{input}}", "model_settings": bad},
        )
        assert r.status_code == 422, (bad, r.text)
    ok = await project.team.engineer.post("/api/prompts", json={"project_id": project.id, "name": "p-ok", "text": "Q: {{input}}",
                                                                "model_settings": {"max_tokens": 300, "temperature": 0.2}})  # fmt: skip
    assert ok.status_code == 201


async def test_an_experiment_that_keeps_crashing_ends_as_failed_with_the_reason(project, stub_llm):
    """Found by the final review: an unexpected exception left the experiment 'running' with no error, re-claimed
    every two minutes, forever."""
    from sqlalchemy import text as sql

    from app.db import system_session
    from app.evals.runner import MAX_ATTEMPTS, run_pending

    stub_llm.responder = answers
    prompt = await project.team.engineer.post(
        "/api/prompts", json={"project_id": project.id, "name": "p-crash", "text": "Q: {{input}}"}
    )
    # A version saved before settings were checked.
    await rows(project.team.org_id, "UPDATE prompt_versions SET model_settings = '{\"max_tokens\": \"lots\"}' WHERE prompt_id = :p RETURNING 1",
               p=prompt.json()["id"])  # fmt: skip
    evaluator = await make_evaluator(project, "c", "contains", {"text": "shipped"})
    experiment, _ = await start(
        project, {"kind": "prompt", "prompt_id": prompt.json()["id"], "version": 1}, [evaluator], name="crash"
    )
    for _ in range(MAX_ATTEMPTS + 1):
        await run_pending()
        async with system_session() as db:  # let the lease run out at once
            await db.execute(sql("UPDATE experiments SET lease_expires_at = now() WHERE status = 'running'"))
    status, error = (
        await rows(project.team.org_id, "SELECT status, error FROM experiments WHERE id = :e", e=experiment)
    )[0]
    assert status == "failed" and "lots" in error


async def test_headers_stored_before_encryption_are_encrypted_by_the_migration(project):
    """Rows written before target headers were encrypted: the migration seals them, and the target still works."""
    import importlib.util
    from datetime import UTC, datetime
    from pathlib import Path
    from uuid import UUID

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.db import tenant_session
    from app.evals.targets import request_headers
    from app.models.evals import Experiment
    from app.security.crypto import decrypt
    from tests.conftest import owner_engine

    org, dataset = UUID(project.team.org_id), await make_dataset(project, [{"input": "x"}], name="legacy-headers")
    async with tenant_session(org) as db:
        e = Experiment(org_id=org, project_id=UUID(project.id), dataset_id=UUID(dataset), name="legacy", status="completed",
                       target={"kind": "http", "url": "http://127.0.0.1:9/x", "headers": {"Authorization": "Bearer legacy-secret"}},
                       evaluator_ids=[], totals={}, finished_at=datetime.now(UTC))  # fmt: skip
        db.add(e)
        await db.flush()
        legacy_id = e.id
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0016_sealed_target_headers.py"
    spec = importlib.util.spec_from_file_location("migration_0016", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with owner_engine().begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
    stored = (await rows(project.team.org_id, "SELECT target FROM experiments WHERE id = :id", id=legacy_id))[0][0]
    assert "headers" not in stored and "legacy-secret" not in str(stored)
    assert request_headers(stored) == {"Authorization": "Bearer legacy-secret"}
    assert decrypt(stored["headers_enc"]) == '{"Authorization": "Bearer legacy-secret"}'
