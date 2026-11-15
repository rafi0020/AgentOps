"""How far a judge agrees with people.

Engineers grade a sample of outputs themselves, on the same 1-5 scale. For each judge this module compares
its grade with the human grade on the same item and output, and reports exact agreement and Cohen's kappa
(agreement corrected for what chance alone would give). A judge with too few labels, or too little
agreement, is "uncalibrated": its scores are still shown, marked as such, and cannot gate a prompt release.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings


def cohen_kappa(a: list[int], b: list[int]) -> float:
    """Cohen's kappa for two raters over the same items. 1 = perfect agreement, 0 = no better than chance."""
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    categories = set(a) | set(b)
    expected = sum((a.count(c) / n) * (b.count(c) / n) for c in categories)
    if expected >= 1.0:  # both raters gave one and the same grade to everything
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


@dataclass
class Calibration:
    n: int
    exact_agreement: float | None
    kappa: float | None
    status: str  # "calibrated" | "uncalibrated"
    why: str
    disagreements: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__


PAIRS = text("""
    SELECT DISTINCT ON (hl.item_id, hl.output_hash) hl.item_id, hl.score AS human,
           CAST(er.scores -> CAST(:evaluator AS text) ->> 'raw' AS int) AS judge,
           er.scores -> CAST(:evaluator AS text) ->> 'reason' AS reason, er.output
    FROM human_labels hl
    JOIN experiment_results er ON er.item_id = hl.item_id AND er.output_hash = hl.output_hash
    WHERE hl.evaluator_id = CAST(:evaluator AS uuid) AND er.scores -> CAST(:evaluator AS text) ->> 'raw' IS NOT NULL
    ORDER BY hl.item_id, hl.output_hash, hl.created_at DESC, er.created_at DESC
""")


async def calibration(db: AsyncSession, evaluator_id: UUID) -> Calibration:
    s = get_settings()
    rows = (await db.execute(PAIRS, {"evaluator": str(evaluator_id)})).all()
    n = len(rows)
    if n == 0:
        return Calibration(0, None, None, "uncalibrated", f"No human labels yet; {s.judge_min_labels} are needed.")
    human, machine = [r.human for r in rows], [r.judge for r in rows]
    agreement, kappa = sum(h == j for h, j in zip(human, machine, strict=True)) / n, cohen_kappa(human, machine)
    different = [
        {"item_id": str(r.item_id), "human": r.human, "judge": r.judge, "judge_reason": r.reason, "output": r.output}
        for r in rows
        if r.human != r.judge
    ][:50]
    if n < s.judge_min_labels:
        status, why = "uncalibrated", f"Only {n} human labels; {s.judge_min_labels} are needed."
    elif kappa < s.judge_min_kappa:
        status, why = (
            "uncalibrated",
            f"Agreement with people is too low (kappa {kappa:.2f}, needs {s.judge_min_kappa:.2f}).",
        )
    else:
        status, why = (
            "calibrated",
            f"Agrees with people on {agreement:.0%} of {n} labelled outputs (kappa {kappa:.2f}).",
        )
    return Calibration(n, round(agreement, 4), round(kappa, 4), status, why, different)
