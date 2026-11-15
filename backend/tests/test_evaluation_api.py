"""The Evaluation screen's data: recorded results, shipped as files, shown as recorded."""


async def test_signed_in_people_see_recorded_results_and_attacks(team):
    body = (await team.viewer.get("/api/eval/results")).json()
    names = {r["name"] for r in body["results"]}
    assert {"test-cc-claude-sonnet-5-5", "test-ag-gpt-oss-120b-medium", "dev-cc-claude-sonnet-5-5"} <= names
    assert isinstance(body["attacks"], list)


async def test_a_run_the_provider_never_answered_is_not_shown_as_a_score(team):
    body = (await team.viewer.get("/api/eval/results")).json()
    assert "unavailable-test-cx-gpt-6-luna" not in {r["name"] for r in body["results"]}
    assert body["unavailable"][0]["model"] == "cx/gpt-6-luna" and "503" in body["unavailable"][0]["note"]


async def test_a_rerun_on_seen_questions_carries_its_note(team):
    body = (await team.viewer.get("/api/eval/results")).json()
    seen = next(r for r in body["results"] if r["name"].startswith("seen-"))
    assert "not a held-out score" in seen["note"]


async def test_results_need_a_signed_in_person(client):
    assert (await client.get("/api/eval/results")).status_code == 401
