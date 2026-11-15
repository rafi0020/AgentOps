"""The Investigator with a real model through 9Router, on the planted bad prompt release."""

import os

import pytest

from app.agent.runtime import advance
from app.config import get_settings
from app.investigator.agent import AGENT
from app.llm import factory
from app.llm.openai_compat import OpenAICompatProvider
from tests.agent_helpers import get_run, get_steps, new_run

pytestmark = pytest.mark.live


@pytest.mark.parametrize("model", ["cc/claude-sonnet-5-5", "cx/gpt-6-luna"])
async def test_investigator_names_the_bad_prompt_release(lumen, monkeypatch, model):
    monkeypatch.setattr(get_settings(), "llm_model", model)
    monkeypatch.setattr(get_settings(), "run_max_seconds", 600)
    factory.set_override(OpenAICompatProvider(os.environ["NINEROUTER_BASE_URL"], os.environ["NINEROUTER_API_KEY"]))

    async def fixed_config(org_id):
        return factory.environment_config()

    monkeypatch.setattr(factory, "load_config", fixed_config)
    try:
        question = "Errors in checkout-assistant rose sharply around 2026-10-14 to 2026-10-16. What caused it?"
        run_id = await new_run(lumen, question=question, role="engineer")
        status = await advance(run_id, AGENT)
    finally:
        factory.set_override(None)
    run = await get_run(lumen, run_id)
    steps = await get_steps(lumen, run_id)
    print(f"\n[{model}] status={status} steps={run.steps_used} tokens={run.tokens_used}")
    print("tools:", [s.name for s in steps if s.kind == "tool"])
    print("errors:", [s.error[:120] for s in steps if s.error])
    print(
        "summary:",
        (run.result or {}).get("summary"),
        "| unsupported:",
        (run.result or {}).get("unsupported"),
        "| error:",
        run.error,
    )
    assert status == "completed", run.error
    report = run.result
    text = (report["summary"] + " " + " ".join(f["claim"] for f in report["findings"])).lower()
    assert "lookup_order" in text and ("version 7" in text or "v7" in text)
    assert report["unsupported"] == []
