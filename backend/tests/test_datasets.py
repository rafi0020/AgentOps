"""Datasets, items and evaluators over HTTP."""

from tests.conftest import make_dataset, send
from tests.otlp_helpers import span, trace_id


async def test_create_fill_list_and_delete_a_dataset(project, other_team):
    team = project.team
    d = await make_dataset(
        project,
        [
            {"input": "Where is order 4417?", "expected": "shipped"},
            {"input": {"q": "refund"}, "metadata": {"tag": "x"}},
        ],
    )
    listed = (await team.viewer.get("/api/datasets", params={"project": project.id})).json()["datasets"]
    assert listed[0]["name"] == "orders" and listed[0]["items"] == 2
    items = (await team.viewer.get(f"/api/datasets/{d}/items")).json()
    assert (
        items["total"] == 2
        and items["items"][0]["expected"] == "shipped"
        and items["items"][1]["input"] == {"q": "refund"}
    )
    assert (
        await team.engineer.post("/api/datasets", json={"project_id": project.id, "name": "orders"})
    ).status_code == 409
    assert (await team.viewer.post("/api/datasets", json={"project_id": project.id, "name": "x"})).status_code == 403
    assert (await team.viewer.post(f"/api/datasets/{d}/items", json=[{"input": "x"}])).status_code == 403
    assert (await other_team.admin.get(f"/api/datasets/{d}/items")).status_code == 404
    assert (await other_team.admin.post(f"/api/datasets/{d}/items", json=[{"input": "x"}])).status_code == 404
    assert (await other_team.admin.get("/api/datasets", params={"project": project.id})).status_code == 404
    assert (await team.engineer.delete(f"/api/datasets/{d}")).status_code == 204
    assert (await team.viewer.get(f"/api/datasets/{d}/items")).status_code == 404


async def test_csv_and_json_upload(project):
    team = project.team
    d = await make_dataset(project, [{"input": "seed"}])
    csv = b"input,expected,source\nWhere is my order?,shipped,email\nCancel it,,chat\n"
    r = await team.engineer.post(f"/api/datasets/{d}/items/upload", files={"file": ("cases.csv", csv, "text/csv")})
    assert r.status_code == 201 and r.json() == {"added": 2}
    items = (await team.viewer.get(f"/api/datasets/{d}/items")).json()["items"]
    assert items[1] | {"id": 0} == {
        "id": 0,
        "input": "Where is my order?",
        "expected": "shipped",
        "metadata": {"source": "email"},
        "source_trace_id": None,
    }
    assert items[2]["expected"] is None
    bad = await team.engineer.post(
        f"/api/datasets/{d}/items/upload", files={"file": ("cases.csv", b"question,answer\na,b\n", "text/csv")}
    )
    assert bad.status_code == 422 and "'input'" in bad.json()["detail"]
    js = await team.engineer.post(
        f"/api/datasets/{d}/items/upload",
        files={"file": ("cases.json", b'[{"input": "a", "expected": "b"}]', "application/json")},
    )
    assert js.json() == {"added": 1}
    assert (
        await team.engineer.post(
            f"/api/datasets/{d}/items/upload", files={"file": ("x.json", b"{not a list", "application/json")}
        )
    ).status_code == 422
    assert (await team.engineer.post(f"/api/datasets/{d}/items", json=[])).status_code == 422


async def test_items_from_traces_need_captured_content(client, project):
    team = project.team
    d = await make_dataset(project, [{"input": "seed"}])
    with_content, without = trace_id(), trace_id()
    await send(client, project, [
        span(trace=with_content, attrs={"gen_ai.prompt": "Where is order 4417?", "gen_ai.completion": "It shipped on Monday."}),
        span(trace=without, attrs={"gen_ai.request.model": "m"}),
    ])  # fmt: skip
    r = await team.engineer.post(f"/api/datasets/{d}/items/from-traces", json={"trace_ids": [with_content, without]})
    assert r.status_code == 201 and r.json() == {"added": 1, "skipped_without_content": 1}
    item = (await team.viewer.get(f"/api/datasets/{d}/items")).json()["items"][-1]
    assert (item["input"], item["expected"], item["source_trace_id"]) == (
        "Where is order 4417?",
        "It shipped on Monday.",
        with_content,
    )
    none = await team.engineer.post(f"/api/datasets/{d}/items/from-traces", json={"trace_ids": [without]})
    assert none.status_code == 422 and "opt in" in none.json()["detail"]


async def test_evaluators_are_validated_when_created(project, other_team):
    team = project.team
    ok = await team.engineer.post(
        "/api/evaluators",
        json={"project_id": project.id, "name": "has-status", "kind": "contains", "config": {"text": "shipped"}},
    )
    assert ok.status_code == 201
    for kind, config in (("regex", {"pattern": "(unclosed"}), ("judge", {"rubric": "short"}), ("vibes", {})):
        r = await team.engineer.post(
            "/api/evaluators", json={"project_id": project.id, "name": f"bad-{kind}", "kind": kind, "config": config}
        )
        assert r.status_code == 422, kind
    assert (
        await team.viewer.post("/api/evaluators", json={"project_id": project.id, "name": "x", "kind": "exact"})
    ).status_code == 403
    assert [
        e["name"]
        for e in (await team.viewer.get("/api/evaluators", params={"project": project.id})).json()["evaluators"]
    ] == ["has-status"]
    assert (await other_team.admin.get("/api/evaluators", params={"project": project.id})).status_code == 404
