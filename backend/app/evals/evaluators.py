"""Evaluators: turn one output into a score between 0 and 1, with a reason.

Deterministic evaluators are cheap, repeatable and say exactly why they failed, so they come first. The
LLM judge (judge.py) is for qualities these cannot express, and its scores are only trusted once it has
been compared with people's labels (calibration.py).
"""

import json
import re
from dataclasses import dataclass

import jsonschema
import regex
from referencing import Registry
from referencing.exceptions import Unresolvable

KINDS = ("exact", "contains", "regex", "numeric", "json_schema", "latency_under", "cost_under", "judge")
DETERMINISTIC = KINDS[:-1]
REGEX_TIMEOUT_S = 0.1
NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class EvaluatorConfigError(ValueError):
    """The configuration cannot work. Raised when an evaluator is created, not while an experiment runs."""


@dataclass
class Score:
    value: float
    passed: bool
    reason: str
    invalid: bool = False  # the evaluator itself failed to produce a judgement
    raw: int | None = None  # a judge's 1-5 grade

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "passed": self.passed,
            "reason": self.reason,
            "invalid": self.invalid,
            "raw": self.raw,
        }


def to_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _verdict(ok: bool, yes: str, no: str) -> Score:
    return Score(1.0 if ok else 0.0, ok, yes if ok else no)


def _outside_refs(node) -> list[str]:
    """Every $ref (or $dynamicRef) that points outside the schema itself. Those would be fetched over the network
    or from disk by the workers, so they are refused."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("$ref", "$dynamicRef") and not (isinstance(value, str) and value.startswith("#")):
                found.append(str(value))
            found += _outside_refs(value)
    elif isinstance(node, list):
        for value in node:
            found += _outside_refs(value)
    return found


def validate_config(kind: str, config: dict) -> None:
    if kind not in KINDS:
        raise EvaluatorConfigError(f"Unknown evaluator kind '{kind}'. Kinds: {', '.join(KINDS)}.")
    try:
        if kind == "regex":
            regex.compile(config["pattern"])
        elif kind == "json_schema":
            jsonschema.Draft202012Validator.check_schema(config["schema"])
            if outside := _outside_refs(config["schema"]):
                raise EvaluatorConfigError(f"A schema may only refer to its own parts ('#/...'), not to {outside[0]}.")
        elif kind == "numeric":
            if float(config.get("tolerance", 0.01)) < 0:
                raise EvaluatorConfigError("tolerance must not be negative.")
        elif kind == "latency_under":
            float(config["ms"])
        elif kind == "cost_under":
            float(config["usd"])
        elif kind == "judge":
            if len(str(config["rubric"]).strip()) < 20:
                raise EvaluatorConfigError(
                    "A judge needs a rubric of at least 20 characters saying what a good answer is."
                )
    except KeyError as exc:
        raise EvaluatorConfigError(f"Missing setting: {exc.args[0]}.") from exc
    except (regex.error, jsonschema.SchemaError, TypeError, ValueError) as exc:
        if isinstance(exc, EvaluatorConfigError):
            raise
        raise EvaluatorConfigError(f"Invalid setting: {exc}") from exc


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in NUMBER.findall(text) if m.replace(",", "").strip("-")]


def evaluate(
    kind: str, config: dict, *, output, expected=None, latency_ms: float | None = None, cost_usd: float | None = None
) -> Score:
    text = to_text(output)
    if kind == "exact":
        return _verdict(
            text.strip() == to_text(expected).strip(),
            "Matches the expected output exactly.",
            "Differs from the expected output.",
        )
    if kind == "contains":
        needle = str(config.get("text") or to_text(expected))
        if not needle:
            return Score(
                0.0,
                False,
                "Nothing to look for: the evaluator has no text and the item has no expected output.",
                invalid=True,
            )
        found = needle in text if config.get("case_sensitive") else needle.lower() in text.lower()
        return _verdict(found, f"Contains '{needle[:60]}'.", f"Does not contain '{needle[:60]}'.")
    if kind == "regex":
        try:
            found = regex.search(config["pattern"], text, timeout=REGEX_TIMEOUT_S) is not None
        except TimeoutError:
            return Score(0.0, False, "The pattern took too long to evaluate on this output.", invalid=True)
        return _verdict(found, "Matches the pattern.", "Does not match the pattern.")
    if kind == "numeric":
        wanted = _numbers(to_text(expected))
        if not wanted:
            return Score(0.0, False, "The expected output contains no number to compare with.", invalid=True)
        tolerance, target = float(config.get("tolerance", 0.01)), wanted[0]
        close = [n for n in _numbers(text) if abs(n - target) <= tolerance * max(1.0, abs(target))]
        return _verdict(
            bool(close),
            f"Contains {close[0] if close else target:g}, within tolerance of {target:g}.",
            f"No number within tolerance of {target:g}.",
        )
    if kind == "json_schema":
        try:
            parsed = json.loads(output) if isinstance(output, str) else output
            if _outside_refs(config["schema"]):  # saved before the check above: never fetch, never count
                return Score(0.0, False, "The schema refers to something outside itself.", invalid=True)
            # An empty registry: references resolve within the schema or not at all, never over the network.
            jsonschema.Draft202012Validator(config["schema"], registry=Registry()).validate(parsed)
        except json.JSONDecodeError:
            return Score(0.0, False, "The output is not valid JSON.")
        except jsonschema.ValidationError as exc:
            return Score(0.0, False, f"Does not fit the schema: {exc.message[:160]}")
        except (Unresolvable, jsonschema.SchemaError) as exc:
            return Score(0.0, False, f"The schema cannot be used: {str(exc)[:160]}", invalid=True)
        return Score(1.0, True, "Valid JSON that fits the schema.")
    if kind == "latency_under":
        if latency_ms is None:
            return Score(0.0, False, "No latency was recorded.", invalid=True)
        return _verdict(
            latency_ms <= float(config["ms"]),
            f"{latency_ms:.0f} ms, within the limit.",
            f"{latency_ms:.0f} ms, over the limit of {float(config['ms']):.0f} ms.",
        )
    if kind == "cost_under":
        if cost_usd is None:
            return Score(0.0, False, "No cost was recorded.", invalid=True)
        return _verdict(
            cost_usd <= float(config["usd"]), f"${cost_usd:.4f}, within the limit.", f"${cost_usd:.4f}, over the limit."
        )
    raise EvaluatorConfigError(f"'{kind}' is not a deterministic evaluator.")
