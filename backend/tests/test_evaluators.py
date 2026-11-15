"""Deterministic evaluators, each on a passing and a failing case."""

import time

import pytest

from app.evals.evaluators import EvaluatorConfigError, evaluate, validate_config


@pytest.mark.parametrize(
    ("kind", "config", "output", "expected", "passes"),
    [
        ("exact", {}, " Paris ", "Paris", True),
        ("exact", {}, "paris", "Paris", False),
        ("contains", {}, "The capital is Paris.", "paris", True),
        ("contains", {"case_sensitive": True}, "The capital is Paris.", "paris", False),
        ("contains", {"text": "refund"}, "We issued a refund today", None, True),
        ("regex", {"pattern": r"^ORD-\d{4}$"}, "ORD-4417", None, True),
        ("regex", {"pattern": r"^ORD-\d{4}$"}, "order 4417", None, False),
        ("numeric", {"tolerance": 0.01}, "The total comes to about 42.0 units.", "42", True),
        ("numeric", {"tolerance": 0.01}, "Revenue was $1,279,400 last year", "1279400", True),
        ("numeric", {"tolerance": 0.01}, "It is 45.", "42", False),
        (
            "json_schema",
            {"schema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
            '{"id": 7}',
            None,
            True,
        ),
        ("json_schema", {"schema": {"type": "object", "required": ["id"]}}, {"name": "x"}, None, False),
        ("json_schema", {"schema": {"type": "object"}}, "not json", None, False),
    ],
)
def test_evaluators_pass_and_fail_as_expected(kind, config, output, expected, passes):
    score = evaluate(kind, config, output=output, expected=expected)
    assert score.passed is passes and score.value == (1.0 if passes else 0.0) and score.reason and not score.invalid


def test_latency_and_cost_limits():
    assert evaluate("latency_under", {"ms": 500}, output="x", latency_ms=320).passed
    slow = evaluate("latency_under", {"ms": 500}, output="x", latency_ms=900)
    assert not slow.passed and "over the limit of 500 ms" in slow.reason
    assert evaluate("cost_under", {"usd": 0.01}, output="x", cost_usd=0.002).passed
    assert not evaluate("cost_under", {"usd": 0.01}, output="x", cost_usd=0.5).passed


def test_what_cannot_be_judged_is_marked_invalid_not_failed_quietly():
    assert evaluate("latency_under", {"ms": 500}, output="x").invalid
    assert evaluate("numeric", {}, output="42", expected="no number here").invalid
    assert evaluate("contains", {}, output="anything", expected=None).invalid


def test_a_catastrophic_pattern_cannot_hang_a_worker():
    """With Python's built-in `re` this pattern runs for minutes. The engine used here either finishes at once
    or is stopped by the 100 ms limit; both are acceptable, hanging is not."""
    started = time.perf_counter()
    score = evaluate("regex", {"pattern": r"(a+)+$"}, output="a" * 40 + "!")
    assert time.perf_counter() - started < 2 and not score.passed


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        ("regex", {"pattern": "(unclosed"}), ("regex", {}), ("json_schema", {"schema": {"type": "nonsense"}}),
        ("latency_under", {"ms": "fast"}), ("cost_under", {}), ("judge", {"rubric": "be good"}), ("vibes", {}),
        ("numeric", {"tolerance": -1}),
    ],
)  # fmt: skip
def test_bad_configuration_is_refused_when_the_evaluator_is_created(kind, config):
    with pytest.raises(EvaluatorConfigError):
        validate_config(kind, config)


def test_good_configuration_is_accepted():
    validate_config("regex", {"pattern": r"\d+"})
    validate_config("judge", {"rubric": "A good answer names the correct order status and is polite."})
    validate_config("exact", {})


# Found by the final review: jsonschema follows "$ref" to any URL, so an evaluator could make the workers fetch
# internal addresses (bypassing the experiment-target checks) or hang ingestion on a host that never answers.
@pytest.mark.parametrize(
    "ref", ["http://169.254.169.254/latest/meta-data", "https://example.com/s.json", "file:///etc/passwd"]
)
def test_a_schema_may_not_refer_outside_itself(ref):
    with pytest.raises(EvaluatorConfigError):
        validate_config("json_schema", {"schema": {"$ref": ref}})
    with pytest.raises(EvaluatorConfigError):
        validate_config("json_schema", {"schema": {"type": "object", "properties": {"a": {"$ref": ref}}}})


def test_local_references_still_work():
    schema = {"$defs": {"id": {"type": "integer"}}, "type": "object", "properties": {"id": {"$ref": "#/$defs/id"}}}
    validate_config("json_schema", {"schema": schema})
    assert evaluate("json_schema", {"schema": schema}, output='{"id": 3}').passed
    assert not evaluate("json_schema", {"schema": schema}, output='{"id": "x"}').passed


def test_a_stored_schema_with_a_remote_reference_is_never_fetched():
    """A schema saved before the check existed: no request is made, and the score is invalid, not an error."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.5)
    port = listener.getsockname()[1]
    score = evaluate("json_schema", {"schema": {"$ref": f"http://127.0.0.1:{port}/s.json"}}, output='{"a": 1}')
    with pytest.raises(TimeoutError):
        listener.accept()  # nothing connected
    listener.close()
    assert score.invalid and not score.passed
