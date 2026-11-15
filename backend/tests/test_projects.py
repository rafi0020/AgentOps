"""Projects and ingestion keys: who may manage them, and that a key is never stored or shown twice."""

from sqlalchemy import text

from app.db import tenant_session
from app.ingest.keys import hash_key


async def test_admin_creates_a_project_and_everyone_in_the_org_can_see_it(team, other_team):
    created = await team.admin.post("/api/projects", json={"name": "support-triage", "retention_days": 14})
    assert created.status_code == 201 and created.json()["retention_days"] == 14
    for client in (team.viewer, team.engineer):
        assert [p["name"] for p in (await client.get("/api/projects")).json()["projects"]] == ["support-triage"]
        assert (await client.post("/api/projects", json={"name": "x"})).status_code == 403
    assert (await other_team.admin.get("/api/projects")).json()["projects"] == []
    assert (await team.admin.post("/api/projects", json={"name": "support-triage"})).status_code == 409
    assert (await team.admin.post("/api/projects", json={"name": "../etc"})).status_code == 422


async def test_key_is_shown_once_and_only_its_hash_is_stored(project):
    team = project.team
    assert project.key.startswith("ak_") and len(project.key) > 40
    listed = (await team.admin.get(f"/api/projects/{project.id}/keys")).json()["keys"]
    assert listed[0]["prefix"] == project.key[:11] and "key" not in listed[0] and listed[0]["scopes"] == ["ingest"]
    async with tenant_session(team.org_id) as db:
        row = (await db.execute(text("SELECT key_hash, prefix FROM ingest_keys"))).one()
        everything = str((await db.execute(text("SELECT ingest_keys::text FROM ingest_keys"))).scalar_one())
    assert row.key_hash == hash_key(project.key) and project.key not in everything
    audit = (await team.admin.get("/api/audit", params={"action": "key."})).text
    assert "key.created" in audit and project.key not in audit


async def test_only_admins_manage_keys(project):
    team = project.team
    for client in (team.viewer, team.engineer):
        assert (await client.get(f"/api/projects/{project.id}/keys")).status_code == 403
        assert (await client.post(f"/api/projects/{project.id}/keys", json={"name": "x"})).status_code == 403
        assert (await client.delete(f"/api/projects/{project.id}/keys/{project.key_id}")).status_code == 403


async def test_keys_and_projects_of_another_organisation_do_not_exist(project, other_team):
    assert (await other_team.admin.get(f"/api/projects/{project.id}/keys")).status_code == 404
    assert (await other_team.admin.post(f"/api/projects/{project.id}/keys", json={"name": "x"})).status_code == 404
    assert (await other_team.admin.delete(f"/api/projects/{project.id}/keys/{project.key_id}")).status_code == 404
    assert (await other_team.admin.patch(f"/api/projects/{project.id}", json={"retention_days": 1})).status_code == 404


async def test_scopes_are_chosen_at_creation_and_validated(project):
    team = project.team
    both = await team.admin.post(
        f"/api/projects/{project.id}/keys", json={"name": "sdk", "scopes": ["prompts:read", "ingest"]}
    )
    assert both.json()["scopes"] == ["ingest", "prompts:read"]
    assert (
        await team.admin.post(f"/api/projects/{project.id}/keys", json={"name": "x", "scopes": ["admin"]})
    ).status_code == 422
    assert (
        await team.admin.post(f"/api/projects/{project.id}/keys", json={"name": "x", "scopes": []})
    ).status_code == 422


async def test_admins_set_model_prices_for_their_organisation(project, other_team):
    team = project.team
    assert (
        await team.engineer.put("/api/prices", json={"model": "m", "input_per_mtok": 1, "output_per_mtok": 2})
    ).status_code == 403
    assert (
        await team.admin.put("/api/prices", json={"model": "m", "input_per_mtok": 1, "output_per_mtok": 2})
    ).status_code == 200
    assert (
        await team.admin.put("/api/prices", json={"model": "m", "input_per_mtok": 1.5, "output_per_mtok": 2})
    ).status_code == 200
    assert [(p["model"], p["input_per_mtok"]) for p in (await team.viewer.get("/api/prices")).json()["prices"]] == [
        ("m", 1.5)
    ]
    assert (await other_team.admin.get("/api/prices")).json()["prices"] == []
    assert (
        await team.admin.put("/api/prices", json={"model": "m", "input_per_mtok": -1, "output_per_mtok": 2})
    ).status_code == 422
