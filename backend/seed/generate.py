"""Generated demo traces for the two demo organisations, with five planted incidents.

    python -m seed.generate                 # about 2 million spans over 30 days
    python -m seed.generate --scale 0.05

Everything here is invented and labelled as such in the UI. It is deterministic: the same seed, scale and end
time produce the same spans. The incidents are described in PLANTED so that the Investigator's answers can be
checked against what was actually put in the data.
"""

import argparse
import asyncio
import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from app.db import dispose_engines, system_session, tenant_session
from app.ingest.partitions import ensure_partitions
from app.ingest.pricing import cost_for
from app.ingest.rollups import add_to_rollups
from app.ingest.writer import write_spans
from app.models.evals import Prompt, PromptVersion, ReleaseRequest
from app.models.projects import Project
from app.redis import close_redis
from seed.demo_users import DEMO_ORGS, LUMEN, ORBIT, ensure_demo_users

DAYS = 30
PRICES = {"model-fast": (Decimal("0.5"), Decimal("1.5")), "model-large": (Decimal("3"), Decimal("15"))}
TRACES_PER_DAY_AT_FULL_SCALE = 3300
CHUNK = 4000

APPS = {
    "checkout-assistant": {"org": LUMEN, "model": "model-fast", "prompt": "order-lookup", "tools": ["lookup_order", "check_stock"], "version": "3.2.0"},
    "support-triage": {"org": LUMEN, "model": "model-fast", "prompt": "triage", "tools": ["fetch_ticket", "search_kb"], "version": "1.9.1"},
    "doc-search": {"org": LUMEN, "model": "model-fast", "prompt": "answer-from-docs", "tools": ["vector_search"], "version": "0.8.4"},
    "billing-bot": {"org": ORBIT, "model": "model-fast", "prompt": "billing", "tools": ["get_invoice"], "version": "2.1.0"},
}  # fmt: skip

# What is planted. Times are offsets from the first day of the data.
PLANTED = {
    "bad_prompt_release": {
        "project": "checkout-assistant", "prompt": "order-lookup", "bad_version": 7, "previous_version": 6, "fixed_version": 8,
        "tool": "lookup_order", "error": "validation error: order_id", "from": timedelta(days=12, hours=10),
        "to": timedelta(days=14, hours=9), "error_rate": 0.35,
    },
    "model_change": {"project": "support-triage", "from_model": "model-fast", "to_model": "model-large", "from": timedelta(days=18, hours=8), "latency_factor": 2.1},
    "agent_loop": {"project": "doc-search", "segment": "enterprise", "from": timedelta(days=22), "to": timedelta(days=24), "model_calls": (14, 18)},
    "provider_outage": {"org": "Lumen Labs", "from": timedelta(days=9, hours=14), "to": timedelta(days=9, hours=14, minutes=40),
                        "errors": ["429 rate limit exceeded", "503 upstream unavailable"], "error_rate": 0.45},
    "latency_drift": {"project": "support-triage", "tool": "fetch_ticket", "from": timedelta(days=24), "daily_growth": 0.05},
}  # fmt: skip


@dataclass
class SeedSummary:
    spans: int = 0
    traces: int = 0
    by_project: dict[str, int] = field(default_factory=dict)
    checksum: str = ""
    start: datetime | None = None
    end: datetime | None = None


def _hex(rng: random.Random, n: int) -> str:
    return f"{rng.getrandbits(n * 4):0{n}x}"


class AppData:
    """Generates one application's traces, day by day."""

    def __init__(self, name: str, project_id, start: datetime, rng: random.Random, scale: float):
        self.name, self.cfg, self.project_id, self.start, self.rng, self.scale = (
            name,
            APPS[name],
            project_id,
            start,
            rng,
            scale,
        )
        self.org = str(self.cfg["org"])

    def _row(
        self,
        trace,
        sid,
        parent,
        name,
        at,
        ms,
        *,
        error=None,
        operation=None,
        model=None,
        tool=None,
        tokens=None,
        prompt=None,
        session=None,
        attrs=None,
    ):
        cost = cost_for(PRICES, model, *(tokens or (None, None)))
        return {
            "org_id": self.org, "project_id": str(self.project_id), "trace_id": trace, "span_id": sid, "parent_span_id": parent,
            "name": name, "kind": 1, "start_time": at.isoformat(), "end_time": (at + timedelta(milliseconds=ms)).isoformat(),
            "duration_ms": round(ms, 3), "status": "error" if error else "ok", "status_message": error, "operation": operation,
            "model": model, "tool": tool, "provider": "acme-ai" if model else None,
            "prompt_name": prompt[0] if prompt else None, "prompt_version": prompt[1] if prompt else None,
            "service_name": self.name, "service_version": self.cfg["version"], "session_id": session,
            "input_tokens": tokens[0] if tokens else None, "output_tokens": tokens[1] if tokens else None,
            "cost_usd": str(cost) if cost is not None else None, "attributes": attrs or {}, "content": None,
            "start_day": at.date().isoformat(),
        }  # fmt: skip

    def trace(self, at: datetime) -> list[dict]:
        rng, cfg, since = self.rng, self.cfg, at - self.start
        release, change, loop, outage, drift = (
            PLANTED[k] for k in ("bad_prompt_release", "model_change", "agent_loop", "provider_outage", "latency_drift")
        )
        model, prompt_version, latency = cfg["model"], 6, 1.0
        if self.name == "checkout-assistant":
            prompt_version = 8 if since >= release["to"] else 7 if since >= release["from"] else 6
        if self.name == "support-triage" and since >= change["from"]:
            model, latency = change["to_model"], change["latency_factor"]
        segment = rng.choices(["smb", "mid-market", "enterprise"], [0.55, 0.3, 0.15])[0]
        looping = self.name == "doc-search" and segment == "enterprise" and loop["from"] <= since < loop["to"]
        in_outage = cfg["org"] == LUMEN and outage["from"] <= since < outage["to"]

        trace, root = _hex(rng, 32), _hex(rng, 16)
        session = f"s-{_hex(rng, 6)}" if rng.random() < 0.3 else None
        rows, cursor = [], at + timedelta(milliseconds=5)
        calls = rng.randint(*loop["model_calls"]) if looping else rng.choices([1, 2, 3], [0.3, 0.5, 0.2])[0]
        for i in range(calls):
            ms = rng.lognormvariate(math.log(650 * latency), 0.45)
            tokens = (rng.randint(600, 2400) + i * 300, rng.randint(60, 420))
            error = (
                rng.choice(outage["errors"])
                if in_outage and rng.random() < outage["error_rate"]
                else ("timeout" if rng.random() < 0.004 else None)
            )
            rows.append(self._row(trace, _hex(rng, 16), root, f"chat {model}", cursor, ms, error=error, operation="chat", model=model,
                                  tokens=tokens, prompt=(cfg["prompt"], prompt_version), session=session))  # fmt: skip
            cursor += timedelta(milliseconds=ms + 8)
            if error:
                break
            tool = rng.choice(cfg["tools"])
            tool_ms = rng.lognormvariate(math.log(90), 0.5)
            if self.name == "support-triage" and tool == drift["tool"] and since >= drift["from"]:
                tool_ms *= (1 + drift["daily_growth"]) ** ((since - drift["from"]).total_seconds() / 86400 + 1)
            tool_error = None
            if (
                self.name == "checkout-assistant"
                and tool == release["tool"]
                and prompt_version == release["bad_version"]
                and rng.random() < release["error_rate"]
            ):
                tool_error = f"{release['error']} '{rng.randint(1000, 9999)}' is not a valid UUID"
            elif rng.random() < 0.008:
                tool_error = "upstream returned 500"
            rows.append(self._row(trace, _hex(rng, 16), root, f"execute_tool {tool}", cursor, tool_ms, error=tool_error,
                                  operation="execute_tool", tool=tool, session=session))  # fmt: skip
            cursor += timedelta(milliseconds=tool_ms + 6)
        total = (cursor - at).total_seconds() * 1000
        rows.insert(0, self._row(trace, root, None, f"invoke_agent {self.name}", at, total, operation="invoke_agent", session=session,
                                 attrs={"customer.segment": segment}))  # fmt: skip
        return rows

    def day(self, index: int) -> list[dict]:
        day_start = self.start + timedelta(days=index)
        n = max(1, round(TRACES_PER_DAY_AT_FULL_SCALE * self.scale * (0.85 if day_start.weekday() >= 5 else 1.0)))
        rows = []
        for _ in range(n):
            hour = min(23.99, max(0.0, self.rng.gauss(13.5, 4.2)))  # busiest in the early afternoon
            rows += self.trace(day_start + timedelta(hours=hour, seconds=self.rng.random() * 3))
        outage = PLANTED["provider_outage"]
        if (
            self.cfg["org"] == LUMEN and index == outage["from"].days
        ):  # make sure the outage window has traffic at any scale
            for _ in range(max(40, n // 30)):
                rows += self.trace(self.start + outage["from"] + timedelta(seconds=self.rng.random() * 2400))
        return rows


async def ensure_projects() -> dict[str, object]:
    ids = {}
    for name, cfg in APPS.items():
        async with tenant_session(cfg["org"]) as db:
            existing = (
                await db.execute(text("SELECT id FROM projects WHERE name = :n"), {"n": name})
            ).scalar_one_or_none()
            if existing is None:
                project = Project(org_id=cfg["org"], name=name)
                db.add(project)
                await db.flush()
                existing = project.id
            ids[name] = existing
            for model, (price_in, price_out) in PRICES.items():
                await db.execute(
                    text(
                        "INSERT INTO prices (org_id, model, input_per_mtok, output_per_mtok) VALUES (:o, :m, :i, :p) ON CONFLICT DO NOTHING"
                    ),
                    {"o": cfg["org"], "m": model, "i": price_in, "p": price_out},
                )
    return ids


async def has_data() -> bool:
    async with system_session() as db:
        return bool(await db.scalar(text("SELECT EXISTS (SELECT 1 FROM projects WHERE name = 'checkout-assistant')")))


async def generate(seed: int = 11, scale: float = 1.0, end: datetime | None = None) -> SeedSummary:
    """Write the demo traces. `end` defaults to the start of today (UTC); the data covers the 30 days before it."""
    await ensure_demo_users()
    end = (end or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=DAYS)
    await ensure_partitions(days_back=(datetime.now(UTC) - start).days + 2, days_ahead=2)
    projects = await ensure_projects()
    summary, digest = SeedSummary(start=start, end=end), hashlib.sha256()
    for index, name in enumerate(APPS):
        app = AppData(name, projects[name], start, random.Random(seed * 100 + index), scale)
        org_id = APPS[name]["org"]
        for day in range(DAYS):
            rows = app.day(day)
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i : i + CHUNK]
                async with tenant_session(org_id) as db:
                    result = await write_spans(db, projects[name], chunk)
                    await add_to_rollups(db, org_id, projects[name], chunk, result)
            for r in rows:
                digest.update(f"{r['trace_id']}{r['span_id']}{r['status']}{r['duration_ms']}".encode())
            summary.spans += len(rows)
            summary.traces += sum(1 for r in rows if r["parent_span_id"] is None)
        summary.by_project[name] = summary.spans - sum(summary.by_project.values())
    await ensure_prompt_history(projects["checkout-assistant"], start)
    summary.checksum = digest.hexdigest()
    return summary


async def ensure_prompt_history(project_id, start: datetime) -> None:
    """The prompt behind incident 1, with the releases that put versions 6, 7 and 8 live, so the Prompts screen
    and the Investigator see the same history the traces show."""
    p = PLANTED["bad_prompt_release"]
    async with tenant_session(LUMEN) as db:
        if await db.scalar(
            text("SELECT 1 FROM prompts WHERE project_id = :p AND name = :n"), {"p": project_id, "n": p["prompt"]}
        ):
            return
        prompt = Prompt(org_id=LUMEN, project_id=project_id, name=p["prompt"], production_version=p["fixed_version"])
        db.add(prompt)
        await db.flush()
        texts = {
            6: "Look up the order with lookup_order(order_id) using the order number the customer gives: {{input}}",
            7: "Look up the order with lookup_order(order_id). Pass the order reference exactly as written by the customer: {{input}}",
            8: "Look up the order with lookup_order(order_id) using the numeric order number only: {{input}}",
        }
        for version in range(1, 9):
            db.add(PromptVersion(org_id=LUMEN, prompt_id=prompt.id, version=version, variables=["input"],
                                 text=texts.get(version, f"Help the customer with their order (draft {version}): {{{{input}}}}")))  # fmt: skip
        await db.flush()
        for from_v, to_v, at in (
            (None, 6, start - timedelta(days=10)),
            (6, 7, start + p["from"]),
            (7, 8, start + p["to"]),
        ):
            release = ReleaseRequest(org_id=LUMEN, prompt_id=prompt.id, from_version=from_v, to_version=to_v, status="approved",
                                     gate={"passed": True, "checks": [{"name": "demo data", "passed": True, "detail": "Generated history."}]})  # fmt: skip
            db.add(release)
            await db.flush()
            await db.execute(
                text("UPDATE release_requests SET created_at = :t, decided_at = :t WHERE id = :id"),
                {"t": at, "id": release.id},
            )


async def main(args: argparse.Namespace) -> None:
    result = await generate(args.seed, args.scale)
    print(
        f"{result.spans:,} spans in {result.traces:,} traces, {result.start:%Y-%m-%d} to {result.end:%Y-%m-%d}; checksum {result.checksum[:12]}"
    )
    for name, count in result.by_project.items():
        print(f"  {name:<22}{count:>10,}")
    assert DEMO_ORGS
    await close_redis()
    await dispose_engines()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--scale", type=float, default=1.0)
    asyncio.run(main(parser.parse_args()))
