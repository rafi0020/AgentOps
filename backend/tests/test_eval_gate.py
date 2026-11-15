"""Regression gate: a recorded held-out result must not fall below its model's baseline, and the question sets
must stay apart. Results for a model with no baseline fail, so a new model is measured before it is relied on."""

import json
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1] / "eval"
BASELINE = json.loads((EVAL / "baseline.json").read_text())["test"]
RESULTS = [json.loads(p.read_text()) for p in sorted((EVAL / "results").glob("test-*.json"))]


def test_a_held_out_result_has_been_recorded_for_two_models():
    assert len({r["model"] for r in RESULTS}) >= 2, "run: python -m eval.run_eval --split test --model <model>"


@pytest.mark.parametrize("result", RESULTS, ids=[r["name"] for r in RESULTS])
def test_recorded_result_is_within_tolerance_of_its_baseline(result):
    assert result["name"] in BASELINE, f"{result['name']} has no baseline in eval/baseline.json"
    for metric, floor in BASELINE[result["name"]].items():
        assert result["metrics"][metric] >= floor - 0.05, f"{result['name']}: {metric} fell below the baseline"


def test_question_sets_do_not_overlap_and_carry_reference_answers():
    dev = json.loads((EVAL / "questions_dev.json").read_text())
    test = json.loads((EVAL / "questions_test.json").read_text())
    assert len(dev) == 27 and len(test) == 13
    assert not {q["question"] for q in dev} & {q["question"] for q in test}
    for q in dev + test:
        assert "expected" in q or "must" in q or "behaviour" in q
        assert ("reference_sql" in q) == ("expected" in q)  # every expected value was computed, none typed


def test_an_evaluation_needs_no_key_for_a_local_endpoint(monkeypatch):
    from eval.run_eval import eval_key

    monkeypatch.delenv("EVAL_API_KEY", raising=False)
    monkeypatch.delenv("NINEROUTER_API_KEY", raising=False)
    assert eval_key() == ""  # Ollama or LM Studio on this machine: no key
    monkeypatch.setenv("EVAL_API_KEY", "local")
    assert eval_key() == "local"
