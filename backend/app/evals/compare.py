"""Side-by-side comparison of two experiments on the same dataset."""

from dataclasses import dataclass, field


@dataclass
class Comparison:
    per_evaluator: dict = field(default_factory=dict)
    items: list = field(default_factory=list)
    regressions: int = 0
    improvements: int = 0


def compare(results_a: dict, results_b: dict, totals_a: dict, totals_b: dict) -> Comparison:
    """`results_*` map item id to that item's scores ({evaluator id: score}); `totals_*` are experiment totals."""
    out = Comparison()
    for evaluator_id in sorted(set(totals_a) | set(totals_b)):
        a, b = (totals_a.get(evaluator_id) or {}).get("mean"), (totals_b.get(evaluator_id) or {}).get("mean")
        out.per_evaluator[evaluator_id] = {
            "mean_a": a,
            "mean_b": b,
            "delta": round(b - a, 4) if a is not None and b is not None else None,
        }
    for item_id in sorted(set(results_a) & set(results_b)):
        a, b = results_a[item_id], results_b[item_id]
        shared = set(a) & set(b)
        passed_a = all(a[e]["passed"] for e in shared) if shared else None
        passed_b = all(b[e]["passed"] for e in shared) if shared else None
        change = "same" if passed_a == passed_b else "regression" if passed_a and not passed_b else "improvement"
        out.regressions += change == "regression"
        out.improvements += change == "improvement"
        out.items.append({"item_id": item_id, "scores_a": a, "scores_b": b, "change": change})
    out.items.sort(
        key=lambda i: {"regression": 0, "improvement": 1, "same": 2}[i["change"]]
    )  # what got worse comes first
    return out
