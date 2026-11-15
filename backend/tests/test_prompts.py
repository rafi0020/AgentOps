"""Prompt registry, release gate and live-prompt reads."""

from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.db import tenant_session
from app.models.evals import Experiment
from app.prompts import gate as gate_module
from app.prompts.registry import MissingVariable, render, variables_in
from client.agentops_client import AgentOps
from tests.conftest import PASSWORD, make_dataset, make_evaluator


async def new_prompt(project, name="order-lookup", text="Answer: {{input}}") -> str:
    r = await project.team.engineer.post("/api/prompts", json={"project_id": project.id, "name": name, "text": text})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def version(project, prompt_id, text) -> int:
    return (await project.team.engineer.post(f"/api/prompts/id/{prompt_id}/versions", json={"text": text})).json()[
        "version"
    ]


async def experiment(
    project, prompt_id, ver, scores: dict, *, dataset=None, status="completed", evaluators=None, graded=25
) -> str:
    """A finished experiment with the given mean score per evaluator, written directly."""
    dataset = dataset or await make_dataset(project, [{"input": "x"}], name=f"d-{ver}-{len(scores)}-{status}")
    org = UUID(project.team.org_id)
    async with tenant_session(org) as db:
        e = Experiment(org_id=org, project_id=UUID(project.id), dataset_id=UUID(dataset), name=f"v{ver}", status=status,
                       target={"kind": "prompt", "prompt_id": prompt_id, "version": ver},
                       evaluator_ids=[UUID(k) for k in (evaluators or scores)],
                       totals={k: {"mean": v, "n": graded, "passed": graded, "invalid": 0, "pass_rate": 1.0} for k, v in scores.items()},
                       finished_at=__import__("datetime").datetime.now(__import__("datetime").UTC))  # fmt: skip
        db.add(e)
        await db.flush()
        return str(e.id)


async def release(project, prompt_id, to_version, experiment_id=None, client=None):
    client = client or project.team.engineer
    return await client.post(
        f"/api/prompts/id/{prompt_id}/releases", json={"to_version": to_version, "experiment_id": experiment_id}
    )


async def live(project, prompt_id) -> int | None:
    return (await project.team.viewer.get(f"/api/prompts/id/{prompt_id}")).json()["production_version"]


def test_rendering_and_variables():
    assert variables_in("Hi {{ name }}, about {{order}} and {{name}}") == ["name", "order"]
    assert render("Order {{order}} for {{ name }}", {"order": 4417, "name": "Ada"}) == "Order 4417 for Ada"
    with pytest.raises(MissingVariable, match="'order'"):
        render("Order {{order}}", {})


async def test_versions_are_added_never_edited(project, other_team):
    team = project.team
    p = await new_prompt(project)
    assert await version(project, p, "Answer politely: {{input}}") == 2
    detail = (await team.viewer.get(f"/api/prompts/id/{p}")).json()
    assert [v["version"] for v in detail["versions"]] == [2, 1] and detail["versions"][1]["text"] == "Answer: {{input}}"
    assert detail["versions"][0]["variables"] == ["input"] and detail["production_version"] is None
    assert (await team.viewer.post(f"/api/prompts/id/{p}/versions", json={"text": "x"})).status_code == 403
    assert (
        await team.engineer.post("/api/prompts", json={"project_id": project.id, "name": "order-lookup", "text": "x"})
    ).status_code == 409
    assert (
        await team.engineer.post("/api/prompts", json={"project_id": project.id, "name": "Bad Name!", "text": "x"})
    ).status_code == 422
    assert (await other_team.admin.get(f"/api/prompts/id/{p}")).status_code == 404


async def test_without_a_gate_a_release_goes_to_an_administrator(project):
    team = project.team
    p = await new_prompt(project)
    r = await release(project, p, 1)
    assert (
        r.status_code == 201
        and r.json()["status"] == "pending"
        and r.json()["gate"]["checks"][0]["name"] == "gate configured"
    )
    assert (
        await team.engineer.post(f"/api/releases/{r.json()['id']}/decide", json={"decision": "approved"})
    ).status_code == 403
    decided = await team.admin.post(f"/api/releases/{r.json()['id']}/decide", json={"decision": "approved"})
    assert decided.json()["status"] == "approved" and await live(project, p) == 1
    again = await team.admin.post(f"/api/releases/{r.json()['id']}/decide", json={"decision": "approved"})
    assert again.status_code == 409


async def test_each_gate_check_can_block_a_release(project):
    team = project.team
    p = await new_prompt(project)
    ev = await make_evaluator(project, "correct", "contains", {"text": "shipped"})
    other_ev = await make_evaluator(project, "other", "exact")
    await team.admin.patch(
        f"/api/prompts/id/{p}/gate", json={"primary_evaluator_id": ev, "min_score": 0.8, "max_drop": 0.02}
    )
    await version(project, p, "v2 {{input}}")
    dataset = await make_dataset(project, [{"input": "x"}], name="gate")

    cases = [
        (None, "experiment"),
        (await experiment(project, p, 1, {ev: 0.95}, dataset=dataset), "experiment tests this version"),
        (await experiment(project, p, 2, {ev: 0.95}, dataset=dataset, status="running"), "experiment finished"),
        (await experiment(project, p, 2, {other_ev: 0.95}, dataset=dataset), "primary evaluator was used"),
        (await experiment(project, p, 2, {ev: 0.70}, dataset=dataset), "minimum score"),
    ]
    for exp, failing in cases:
        body = (await release(project, p, 2, exp)).json()
        assert body["status"] == "blocked", failing
        assert [c["name"] for c in body["gate"]["checks"] if not c["passed"]] == [failing]
        assert (
            await team.admin.post(f"/api/releases/{body['id']}/decide", json={"decision": "approved"})
        ).status_code == 409

    passing = (await release(project, p, 2, await experiment(project, p, 2, {ev: 0.9}, dataset=dataset))).json()
    assert passing["status"] == "pending" and all(c["passed"] for c in passing["gate"]["checks"]), passing["gate"]
    assert (
        await team.admin.post(f"/api/releases/{passing['id']}/decide", json={"decision": "approved"})
    ).status_code == 200
    assert await live(project, p) == 2

    # Version 3 must not fall more than 0.02 below the live version on the same dataset.
    await experiment(project, p, 2, {ev: 0.95}, dataset=dataset)  # the live version's most recent result
    await version(project, p, "v3 {{input}}")
    worse = (await release(project, p, 3, await experiment(project, p, 3, {ev: 0.90}, dataset=dataset))).json()
    assert worse["status"] == "blocked" and "live version 2 scored 0.950" in str(worse["gate"])
    fine = (await release(project, p, 3, await experiment(project, p, 3, {ev: 0.94}, dataset=dataset))).json()
    assert fine["status"] == "pending"

    assert (
        await team.admin.post(f"/api/releases/{fine['id']}/decide", json={"decision": "approved"})
    ).status_code == 200
    assert await live(project, p) == 3

    # Going back to a version that was live before needs no new experiment; one that never was, does.
    back = (await release(project, p, 2)).json()
    assert back["status"] == "pending" and back["gate"]["checks"][0]["name"] == "rollback"
    never = (await release(project, p, 1)).json()
    assert never["status"] == "blocked" and never["gate"]["checks"][0]["name"] == "experiment"


async def test_a_release_needs_enough_graded_items_not_just_a_good_mean(project):
    team = project.team
    p = await new_prompt(project)
    ev = await make_evaluator(project, "correct", "contains", {"text": "shipped"})
    await team.admin.patch(f"/api/prompts/id/{p}/gate", json={"primary_evaluator_id": ev, "min_score": 0.5})
    await version(project, p, "v2 {{input}}")
    dataset = await make_dataset(project, [{"input": "x"}], name="coverage")
    thin = await experiment(project, p, 2, {ev: 1.0}, dataset=dataset, graded=3)  # a perfect mean over three items
    body = (await release(project, p, 2, thin)).json()
    assert body["status"] == "blocked"
    assert [c["name"] for c in body["gate"]["checks"] if not c["passed"]] == ["graded items"]
    assert "3 items were graded" in str(body["gate"])
    enough = (await release(project, p, 2, await experiment(project, p, 2, {ev: 1.0}, dataset=dataset))).json()
    assert enough["status"] == "pending"


async def test_an_uncalibrated_judge_cannot_carry_a_release(project, monkeypatch):
    team = project.team
    p = await new_prompt(project)
    judge = await make_evaluator(project, "judge", "judge", {"rubric": "The answer gives the correct order status."})
    await team.admin.patch(f"/api/prompts/id/{p}/gate", json={"primary_evaluator_id": judge, "min_score": 0.5})
    exp = await experiment(project, p, 1, {judge: 0.9})
    blocked = (await release(project, p, 1, exp)).json()
    failed = [c for c in blocked["gate"]["checks"] if not c["passed"]]
    assert (
        blocked["status"] == "blocked"
        and failed[0]["name"] == "judge 'judge' is calibrated"
        and "No human labels" in failed[0]["detail"]
    )

    calibrated = SimpleNamespace(status="calibrated", why="Agrees with people.")

    async def fake(db, evaluator_id):
        return calibrated

    monkeypatch.setattr(gate_module, "calibration", fake)
    ok = (await release(project, p, 1, exp)).json()
    assert ok["status"] == "pending"
    calibrated.status, calibrated.why = (
        "uncalibrated",
        "Agreement with people is too low.",
    )  # changed before anyone decided
    r = await team.admin.post(f"/api/releases/{ok['id']}/decide", json={"decision": "approved"})
    assert r.status_code == 409 and await live(project, p) is None
    assert (await team.viewer.get("/api/releases", params={"status": "blocked"})).json()["releases"][0]["id"] == ok[
        "id"
    ]


async def test_two_people_for_a_release_unless_there_is_only_one_administrator(project):
    team = project.team
    p = await new_prompt(project)
    own = (await release(project, p, 1, client=team.admin)).json()
    alone = await team.admin.post(f"/api/releases/{own['id']}/decide", json={"decision": "approved"})
    assert alone.status_code == 200
    entry = (await team.admin.get("/api/audit", params={"action": "release.approved"})).json()["entries"][0]
    assert entry["detail"]["sole_admin"] is True

    await team.admin.post(
        "/api/users",
        json={"email": f"second-{p[:8]}@example.test", "name": "Second", "role": "admin", "password": PASSWORD},
    )
    await version(project, p, "v2 {{input}}")
    own2 = (await release(project, p, 2, client=team.admin)).json()
    assert (
        await team.admin.post(f"/api/releases/{own2['id']}/decide", json={"decision": "approved"})
    ).status_code == 403
    second = await team.login(f"second-{p[:8]}@example.test")
    rejected = await second.post(
        f"/api/releases/{own2['id']}/decide", json={"decision": "rejected", "reason": "not yet"}
    )
    assert rejected.json()["status"] == "rejected" and await live(project, p) == 1


async def test_applications_read_the_live_prompt_with_a_scoped_key(client, project, other_team):
    team = project.team
    p = await new_prompt(project)
    reader = (
        await team.admin.post(f"/api/projects/{project.id}/keys", json={"name": "reader", "scopes": ["prompts:read"]})
    ).json()["key"]
    auth = {"Authorization": f"Bearer {reader}"}
    assert (await client.get("/v1/prompts/order-lookup", headers=auth)).status_code == 404  # nothing live yet
    rid = (await release(project, p, 1)).json()["id"]
    await team.admin.post(f"/api/releases/{rid}/decide", json={"decision": "approved"})

    r = await client.get("/v1/prompts/order-lookup", headers=auth)
    assert r.json() == {
        "name": "order-lookup",
        "version": 1,
        "text": "Answer: {{input}}",
        "variables": ["input"],
        "model_settings": {},
    }
    assert (
        await client.get("/v1/prompts/order-lookup", headers=auth | {"If-None-Match": r.headers["ETag"]})
    ).status_code == 304
    assert (await client.get("/v1/prompts/order-lookup", headers=project.auth)).status_code == 403  # an ingest-only key

    other_project = (await other_team.admin.post("/api/projects", json={"name": "elsewhere"})).json()["id"]
    other_key = (
        await other_team.admin.post(
            f"/api/projects/{other_project}/keys", json={"name": "r", "scopes": ["prompts:read"]}
        )
    ).json()["key"]
    assert (
        await client.get("/v1/prompts/order-lookup", headers={"Authorization": f"Bearer {other_key}"})
    ).status_code == 404


def test_client_caches_and_keeps_working_when_the_server_is_down():
    calls = []

    def server(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("if-none-match"))
        if len(calls) >= 3:
            raise httpx.ConnectError("down")
        if request.headers.get("if-none-match") == '"p-1"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={"ETag": '"p-1"'},
            json={"name": "greet", "version": 1, "text": "Hello {{name}}", "variables": ["name"], "model_settings": {}},
        )

    ops = AgentOps("http://ops.test", "ak_x", cache_seconds=0, http=httpx.Client(transport=httpx.MockTransport(server)))
    first = ops.get_prompt("greet")
    assert first.render(name="Ada") == "Hello Ada" and ops.get_prompt("greet").version == 1  # second call: 304
    assert ops.get_prompt("greet").text == "Hello {{name}}"  # third call: server down, last good copy
    assert calls == [None, '"p-1"', '"p-1"']
    with pytest.raises(ValueError, match="name"):
        first.render()
