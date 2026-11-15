"""The release gate: what must be true before a prompt version can become the live one.

The gate does not decide; an administrator does. It decides whether the request may be put in front of an
administrator at all. Each check is recorded with the request, so the reason a release was blocked, or what
it was approved on, can be read later.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.evals.calibration import calibration
from app.models.evals import Evaluator, Experiment, Prompt, ReleaseRequest


@dataclass
class GateResult:
    passed: bool = True
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append({"name": name, "passed": ok, "detail": detail})
        self.passed = self.passed and ok
        return ok

    def as_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks}


def _mean(experiment: Experiment, evaluator_id: UUID) -> float | None:
    return ((experiment.totals or {}).get(str(evaluator_id)) or {}).get("mean")


async def check_gate(db: AsyncSession, prompt: Prompt, to_version: int, experiment: Experiment | None) -> GateResult:
    gate = GateResult()
    was_live = (
        await db.execute(
            select(ReleaseRequest.id).where(ReleaseRequest.prompt_id == prompt.id, ReleaseRequest.to_version == to_version, ReleaseRequest.status == "approved").limit(1)
        )
    ).first()  # fmt: skip
    if was_live and experiment is None:
        gate.add(
            "rollback", True, f"Version {to_version} has been live before; returning to it needs no new experiment."
        )
        return gate
    if prompt.primary_evaluator_id is None:
        gate.add(
            "gate configured",
            True,
            "No primary evaluator is set for this prompt, so nothing is checked. An administrator still decides.",
        )
        return gate
    if experiment is None:
        gate.add("experiment", False, "Name the experiment that tested this version.")
        return gate

    target = experiment.target or {}
    on_this = (
        target.get("kind") == "prompt"
        and target.get("prompt_id") == str(prompt.id)
        and target.get("version") == to_version
    )
    if not gate.add("experiment tests this version", on_this, f"The experiment ran version {target.get('version')} of this prompt." if on_this
                    else f"The experiment did not run version {to_version} of this prompt."):  # fmt: skip
        return gate
    if not gate.add("experiment finished", experiment.status == "completed", f"The experiment is {experiment.status}."):
        return gate
    score = _mean(experiment, prompt.primary_evaluator_id)
    if not gate.add("primary evaluator was used", score is not None, "The experiment includes the prompt's primary evaluator." if score is not None
                    else "The experiment did not use the prompt's primary evaluator, or it scored nothing."):  # fmt: skip
        return gate

    # A mean over three items is not evidence; the primary evaluator must have graded enough of them.
    graded = ((experiment.totals or {}).get(str(prompt.primary_evaluator_id)) or {}).get("n", 0)
    needed = get_settings().gate_min_graded
    gate.add("graded items", graded >= needed, f"{graded} items were graded; {needed} are needed.")
    if prompt.min_score is not None:
        gate.add(
            "minimum score", score >= prompt.min_score, f"Scored {score:.3f}; the minimum is {prompt.min_score:.3f}."
        )
    if prompt.production_version is not None and prompt.production_version != to_version:
        baseline = (
            await db.execute(
                select(Experiment)
                .where(Experiment.project_id == prompt.project_id, Experiment.dataset_id == experiment.dataset_id, Experiment.status == "completed",
                       Experiment.target["prompt_id"].astext == str(prompt.id), Experiment.target["version"].astext == str(prompt.production_version))
                .order_by(Experiment.finished_at.desc()).limit(1)
            )
        ).scalar_one_or_none()  # fmt: skip
        base = _mean(baseline, prompt.primary_evaluator_id) if baseline else None
        if base is None:
            gate.add(
                "against the live version",
                True,
                f"The live version {prompt.production_version} has no experiment on this dataset to compare with.",
            )
        else:
            gate.add("against the live version", score >= base - prompt.max_drop,
                     f"Scored {score:.3f}; the live version {prompt.production_version} scored {base:.3f} (allowed drop {prompt.max_drop:.3f}).")  # fmt: skip

    judges = (
        (
            await db.execute(
                select(Evaluator).where(Evaluator.id.in_(experiment.evaluator_ids), Evaluator.kind == "judge")
            )
        )
        .scalars()
        .all()
    )
    for evaluator in judges:
        c = await calibration(db, evaluator.id)
        # A judge nobody has checked against people is an opinion, not a measurement: it cannot carry a release.
        gate.add(f"judge '{evaluator.name}' is calibrated", c.status == "calibrated", c.why)
    return gate
