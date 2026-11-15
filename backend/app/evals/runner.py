"""Runs an experiment: every dataset item through the target, then through each evaluator.

Experiments are claimed under a lease like agent runs. Each item's result is committed on its own, and an
item that already has a result is skipped, so an experiment interrupted by a crash or a deploy continues with
the items that are left and never scores one twice.
"""

import asyncio
import hashlib
import json
import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.db import system_session, tenant_session
from app.evals.evaluators import Score, evaluate, to_text
from app.evals.judge import judge
from app.evals.targets import TargetError, call_http, call_prompt
from app.llm import factory
from app.llm.base import LLMError
from app.models.evals import DatasetItem, Evaluator, Experiment, ExperimentResult, PromptVersion

log = logging.getLogger("agentops.experiments")
CONCURRENCY = 4
MAX_ATTEMPTS = 3  # an experiment that crashes the worker this often is failed, with the reason

CLAIM = text("""
WITH candidate AS (
    SELECT id FROM experiments
    WHERE status = 'queued' OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= now()))
    ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE experiments e SET status = 'running', lease_owner = :worker, attempts = e.attempts + 1,
       lease_expires_at = now() + make_interval(secs => :lease)
FROM candidate WHERE e.id = candidate.id RETURNING e.id, e.org_id, e.attempts
""")


def output_hash(output) -> str:
    return hashlib.sha256(to_text(output).encode()).hexdigest()[:32]


async def claim_experiment(worker_id: str, lease_s: int = 120):
    async with system_session() as db:
        return (await db.execute(CLAIM, {"worker": worker_id, "lease": lease_s})).first()


async def _finish(org_id: UUID, experiment_id: UUID, status: str, error: str | None = None) -> str:
    async with tenant_session(org_id) as db:
        rows = (
            (await db.execute(select(ExperimentResult.scores).where(ExperimentResult.experiment_id == experiment_id)))
            .scalars()
            .all()
        )
        totals: dict[str, dict] = {}
        for scores in rows:
            for evaluator_id, s in scores.items():
                t = totals.setdefault(evaluator_id, {"n": 0, "sum": 0.0, "passed": 0, "invalid": 0})
                if s.get("invalid"):
                    t["invalid"] += 1
                else:
                    t["n"], t["sum"], t["passed"] = t["n"] + 1, t["sum"] + s["value"], t["passed"] + bool(s["passed"])
        for t in totals.values():
            t["mean"] = round(t.pop("sum") / t["n"], 4) if t["n"] else None
            t["pass_rate"] = round(t["passed"] / t["n"], 4) if t["n"] else None
        await db.execute(
            update(Experiment)
            .where(Experiment.id == experiment_id)
            .values(
                status=status,
                error=error,
                totals=totals,
                finished_at=func.now(),
                lease_owner=None,
                lease_expires_at=None,
            )
        )
    return status


async def advance_experiment(experiment_id: UUID, org_id: UUID, *, transport=None) -> str:
    """Process the items that have no result yet. Safe to call again at any time."""
    s = get_settings()
    async with tenant_session(org_id) as db:
        experiment = (await db.execute(select(Experiment).where(Experiment.id == experiment_id))).scalar_one()
        if experiment.status != "running":
            return experiment.status
        evaluators = (
            (await db.execute(select(Evaluator).where(Evaluator.id.in_(experiment.evaluator_ids)))).scalars().all()
        )
        done = select(ExperimentResult.item_id).where(ExperimentResult.experiment_id == experiment_id)
        items = (
            await db.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == experiment.dataset_id, DatasetItem.id.not_in(done)).order_by(DatasetItem.seq)
            )
        ).scalars().all()  # fmt: skip
        target = dict(experiment.target)
        version = None
        if target["kind"] == "prompt":
            version = (
                await db.execute(select(PromptVersion).where(PromptVersion.prompt_id == UUID(target["prompt_id"]), PromptVersion.version == target["version"]))
            ).scalar_one_or_none()  # fmt: skip
            if version is None:
                return await _finish(
                    org_id, experiment_id, "failed", "The prompt version this experiment targets no longer exists."
                )
    needs_model = target["kind"] == "prompt" or any(e.kind == "judge" for e in evaluators)
    try:
        client = await factory.get_client(org_id) if needs_model else None
    except ValueError as exc:
        return await _finish(org_id, experiment_id, "failed", f"The model is not configured correctly: {exc}")
    gate, stop_reason = asyncio.Semaphore(CONCURRENCY), None

    async def one(item: DatasetItem) -> None:
        nonlocal stop_reason
        async with gate:
            if stop_reason:
                return
            async with tenant_session(org_id) as db:
                state = (
                    await db.execute(
                        select(Experiment.cancel_requested, Experiment.cost_usd).where(Experiment.id == experiment_id)
                    )
                ).one()
            if state.cancel_requested:
                stop_reason = "cancelled"
                return
            if float(state.cost_usd) >= s.experiment_max_cost_usd:
                stop_reason = (
                    f"Stopped: the cost limit of ${s.experiment_max_cost_usd:.2f} for one experiment was reached."
                )
                return
            output, latency, cost, error = None, None, Decimal(0), None
            try:
                if target["kind"] == "prompt":
                    produced = await call_prompt(client, version.text, version.model_settings, item.input)
                else:
                    produced = await call_http(target, item.input, transport=transport)
                output, latency, cost = produced.output, produced.latency_ms, produced.cost_usd
            except (TargetError, LLMError) as exc:
                error = str(exc)[:500]
            scores = {}
            for evaluator in evaluators:
                if error:
                    result = Score(0.0, False, "The target produced no output.", invalid=False)
                elif evaluator.kind == "judge":
                    try:
                        result, judge_cost = await judge(
                            client, evaluator.config["rubric"], input=item.input, output=output, expected=item.expected
                        )
                        cost += judge_cost
                    except LLMError as exc:
                        result = Score(0.0, False, f"The judge could not be called: {exc}"[:300], invalid=True)
                else:
                    result = evaluate(
                        evaluator.kind,
                        evaluator.config,
                        output=output,
                        expected=item.expected,
                        latency_ms=latency,
                        cost_usd=float(cost),
                    )
                scores[str(evaluator.id)] = result.as_dict()
            async with tenant_session(org_id) as db:
                inserted = await db.execute(
                    insert(ExperimentResult)
                    .values(
                        org_id=org_id,
                        experiment_id=experiment_id,
                        item_id=item.id,
                        output=json.loads(json.dumps(output, default=str)),
                        output_hash=output_hash(output) if error is None else None,
                        latency_ms=latency,
                        cost_usd=cost,
                        error=error,
                        scores=scores,
                    )  # fmt: skip
                    .on_conflict_do_nothing(index_elements=["experiment_id", "item_id"])
                    .returning(ExperimentResult.id)
                )
                if inserted.first() is not None:
                    await db.execute(
                        update(Experiment)
                        .where(Experiment.id == experiment_id)
                        .values(items_done=Experiment.items_done + 1, cost_usd=Experiment.cost_usd + cost, lease_expires_at=func.now() + text("interval '120 seconds'"))
                    )  # fmt: skip

    await asyncio.gather(*[one(item) for item in items])
    if stop_reason == "cancelled":
        return await _finish(org_id, experiment_id, "cancelled")
    if stop_reason:
        return await _finish(org_id, experiment_id, "failed", stop_reason)
    return await _finish(org_id, experiment_id, "completed")


async def run_pending(worker_id: str = "experiments", *, transport=None) -> int:
    """Advance every claimable experiment. Used by the worker loop and by tests."""
    n = 0
    while (claimed := await claim_experiment(worker_id)) is not None:
        try:
            await advance_experiment(claimed.id, claimed.org_id, transport=transport)
        except Exception as exc:  # noqa: BLE001 - retried under the lease, then failed with the reason shown
            log.exception(
                "experiment failed in worker", extra={"experiment": str(claimed.id), "attempt": claimed.attempts}
            )
            if claimed.attempts >= MAX_ATTEMPTS:
                reason = f"Stopped after {claimed.attempts} attempts: {type(exc).__name__}: {exc}"[:500]
                await _finish(claimed.org_id, claimed.id, "failed", reason)
        n += 1
    return n


async def loop(stop: asyncio.Event, worker_id: str, poll_s: float = 2.0) -> None:
    while not stop.is_set():
        try:
            await run_pending(worker_id)
        except Exception:  # noqa: BLE001
            log.exception("experiment loop error")
        try:
            await asyncio.wait_for(stop.wait(), poll_s)
        except TimeoutError:
            pass
