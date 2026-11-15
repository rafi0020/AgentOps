"""The Investigator's tools for reading what applications did: metrics, traces, errors and changes.

There is no free-form query tool. Each tool is a fixed, organisation-scoped read with bounded output, so the
model can only ask questions these tools can answer, and every result is stored as evidence its report must
cite. Text that came from the observed applications (span names, error messages, content) is returned under
`untrusted_data`: it is what the applications wrote, not instructions.
"""

import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text

from app.agent.tools import ToolContext, ToolError, ToolResult
from app.db import tenant_session
from app.ingest.rollups import DIMENSIONS, query_rollups
from app.investigator.common import PREVIEW_ROWS, _store, registry
from app.models.evals import Prompt, PromptVersion, ReleaseRequest
from app.models.projects import Project
from app.models.runs import new_handle

MAX_RANGE = timedelta(days=31)
GROUPABLE = Literal["model", "operation", "tool", "prompt_name", "prompt_version", "service_version"]


def _must_have_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("include a timezone, e.g. Z")
    return value


class Window(BaseModel):
    project: str = Field(description="Project name, as listed by list_projects")
    start: datetime = Field(description="ISO timestamp with timezone, e.g. 2026-11-01T00:00:00Z")
    end: datetime = Field(description="ISO timestamp with timezone, after start")

    _aware = field_validator("start", "end")(_must_have_timezone)


def _check_range(start: datetime, end: datetime) -> None:
    if start >= end:
        raise ToolError("start must be before end.")
    if end - start > MAX_RANGE:
        raise ToolError("A time range may be at most 31 days. Narrow it.")


async def _project(ctx: ToolContext, name: str) -> Project:
    async with tenant_session(ctx.org_id) as db:
        project = (await db.execute(select(Project).where(Project.name == name))).scalar_one_or_none()
        if project is None:
            names = (await db.execute(select(Project.name).order_by(Project.name))).scalars().all()
    if project is None:
        raise ToolError(f"There is no project named '{name}'. Projects: {', '.join(names) or 'none'}.")
    return project


def _table(rows: list[dict]) -> dict:
    columns = list(rows[0]) if rows else []
    return {"columns": columns, "rows": [[r.get(c) for c in columns] for r in rows]}


def _result(
    ctx: ToolContext,
    summary: str,
    rows: list[dict],
    purpose: str,
    *,
    extra: dict | None = None,
    untrusted: bool = False,
) -> ToolResult:
    handle = new_handle()
    table = _table(rows)
    shown = rows[:PREVIEW_ROWS]
    data = ({"untrusted_data": shown} if untrusted else {"rows": shown}) | (extra or {})
    if len(rows) > PREVIEW_ROWS:
        summary += f" Showing the first {PREVIEW_ROWS} of {len(rows)}; the handle refers to all of them."
    return ToolResult(
        summary,
        data=data,
        handle=handle,
        commit=_store(ctx, handle, "query", table | {"purpose": purpose}, rows=len(rows)),
    )


# ------------------------------------------------------------------ projects and metrics


class NoArgs(BaseModel):
    pass


@registry.tool("list_projects", "List the projects (observed applications) of this organisation.")
async def list_projects(ctx: ToolContext, args: NoArgs) -> ToolResult:
    async with tenant_session(ctx.org_id) as db:
        rows = (await db.execute(select(Project.name, Project.retention_days).order_by(Project.name))).all()
    return ToolResult(
        f"{len(rows)} project(s).", data={"projects": [{"name": n, "retention_days": d} for n, d in rows]}
    )


class MetricsArgs(Window):
    group_by: list[GROUPABLE] = Field(default_factory=list, max_length=3)
    step: Literal["5m", "1h", "1d"] | None = Field(default=None, description="Add a time column at this step")
    model: str | None = None
    tool: str | None = None
    operation: str | None = None


@registry.tool(
    "query_metrics",
    "Calls, errors, error_rate, roots (traces), tokens, cost_usd, p50_ms and p95_ms for a project and time range, "
    "optionally grouped by model, operation, tool, prompt_name, prompt_version or service_version, and by time step. "
    "Latency percentiles are the upper bound of a histogram bucket.",
)
async def query_metrics(ctx: ToolContext, args: MetricsArgs) -> ToolResult:
    _check_range(args.start, args.end)
    project = await _project(ctx, args.project)
    filters = {"model": args.model, "tool": args.tool, "operation": args.operation}
    async with tenant_session(ctx.org_id) as db:
        rows = await query_rollups(db, project.id, args.start, args.end, list(args.group_by), args.step, filters)
    rows = [r for r in rows if r["calls"]]
    if not rows:
        return _result(
            ctx, "No calls in this range for these filters.", [], f"metrics for {args.project}", untrusted=True
        )
    return _result(
        ctx,
        f"{len(rows)} row(s).",
        rows,
        f"metrics for {args.project} {args.start:%Y-%m-%d %H:%M} to {args.end:%Y-%m-%d %H:%M}",
        untrusted=True,  # model, tool and version names are written by the applications
    )


class CompareArgs(BaseModel):
    project: str
    a_start: datetime
    a_end: datetime
    b_start: datetime
    b_end: datetime
    group_by: list[GROUPABLE] = Field(default_factory=list, max_length=2)
    operation: str | None = None

    _aware = field_validator("a_start", "a_end", "b_start", "b_end")(_must_have_timezone)


@registry.tool(
    "compare_windows",
    "Compare two time ranges (a = before, b = after) for a project, optionally grouped. Returns each group's figures "
    "in both ranges and the differences, calculated in code: error_rate_change, p95_ratio, cost_per_trace_ratio, calls_ratio.",
)
async def compare_windows(ctx: ToolContext, args: CompareArgs) -> ToolResult:
    for start, end in ((args.a_start, args.a_end), (args.b_start, args.b_end)):
        _check_range(start, end)
    project = await _project(ctx, args.project)
    async with tenant_session(ctx.org_id) as db:
        a = await query_rollups(
            db, project.id, args.a_start, args.a_end, list(args.group_by), filters={"operation": args.operation}
        )
        b = await query_rollups(
            db, project.id, args.b_start, args.b_end, list(args.group_by), filters={"operation": args.operation}
        )

    def key(r):
        return tuple(r.get(g) for g in args.group_by)

    def ratio(x, y):
        return round(y / x, 4) if x and y is not None else None

    by_a, by_b = {key(r): r for r in a if r["calls"]}, {key(r): r for r in b if r["calls"]}
    rows = []
    for k in sorted(set(by_a) | set(by_b), key=str):
        ra, rb = by_a.get(k, {}), by_b.get(k, {})
        cost_a = ra["cost_usd"] / ra["roots"] if ra.get("roots") else None
        cost_b = rb["cost_usd"] / rb["roots"] if rb.get("roots") else None
        rows.append(
            dict(zip(args.group_by, k, strict=True))
            | {
                "calls_a": ra.get("calls", 0),
                "calls_b": rb.get("calls", 0),
                "error_rate_a": ra.get("error_rate"),
                "error_rate_b": rb.get("error_rate"),
                "error_rate_change": round(rb["error_rate"] - ra["error_rate"], 6)
                if ra.get("error_rate") is not None and rb.get("error_rate") is not None
                else None,
                "p95_ms_a": ra.get("p95_ms"),
                "p95_ms_b": rb.get("p95_ms"),
                "p95_ratio": ratio(ra.get("p95_ms"), rb.get("p95_ms")),
                "cost_per_trace_a": round(cost_a, 6) if cost_a is not None else None,
                "cost_per_trace_b": round(cost_b, 6) if cost_b is not None else None,
                "cost_per_trace_ratio": ratio(cost_a, cost_b),
                "calls_ratio": ratio(ra.get("calls"), rb.get("calls")),
            }  # fmt: skip
        )
    rows.sort(key=lambda r: abs(r["error_rate_change"] or 0), reverse=True)
    return _result(
        ctx,
        f"{len(rows)} group(s) compared; sorted by the largest change in error rate.",
        rows,
        f"comparison for {args.project}",
        untrusted=True,
    )


# ------------------------------------------------------------------ traces and errors


class SearchArgs(Window):
    status: Literal["ok", "error"] | None = None
    model: str | None = None
    tool: str | None = None
    min_duration_ms: float | None = None
    limit: int = Field(default=10, ge=1, le=20)


@registry.tool(
    "search_traces", "Find traces in a range by status, model, tool or minimum duration. Returns a count and a sample."
)
async def search_traces(ctx: ToolContext, args: SearchArgs) -> ToolResult:
    _check_range(args.start, args.end)
    project = await _project(ctx, args.project)
    where, params = (
        ["project_id = :p", "start_time >= :s", "start_time < :e"],
        {"p": project.id, "s": args.start, "e": args.end, "n": args.limit},
    )
    for clause, name, value in (("status = :status", "status", args.status), (":model = ANY(models)", "model", args.model),
                                (":tool = ANY(tools)", "tool", args.tool), ("duration_ms >= :ms", "ms", args.min_duration_ms)):  # fmt: skip
        if value is not None:
            where.append(clause)
            params[name] = value
    sql = " AND ".join(where)
    async with tenant_session(ctx.org_id) as db:
        total = await db.scalar(text(f"SELECT count(*) FROM traces WHERE {sql}"), params)  # noqa: S608 - fixed clauses
        found = (
            await db.execute(text(f"SELECT * FROM traces WHERE {sql} ORDER BY start_time DESC LIMIT :n"), params)
        ).all()  # noqa: S608
    rows = [
        {"trace_id": r.trace_id, "root_name": r.root_name, "status": r.status, "start_time": r.start_time.isoformat(), "duration_ms": round(r.duration_ms),
         "spans": r.span_count, "errors": r.error_count, "cost_usd": float(r.cost_usd), "models": r.models, "tools": r.tools}
        for r in found
    ]  # fmt: skip
    return _result(
        ctx,
        f"{total} trace(s) match; {len(rows)} shown, newest first.",
        rows,
        f"trace search in {args.project}",
        extra={"total": total},
        untrusted=True,
    )


class TraceArgs(BaseModel):
    project: str
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")


@registry.tool(
    "get_trace",
    "Read one trace: its spans in order, with timing, status, error messages, model, tool and key attributes.",
)
async def get_trace(ctx: ToolContext, args: TraceArgs) -> ToolResult:
    project = await _project(ctx, args.project)
    async with tenant_session(ctx.org_id) as db:
        spans = (
            await db.execute(
                text("SELECT * FROM spans WHERE project_id = :p AND trace_id = :t ORDER BY start_time LIMIT 61"),
                {"p": project.id, "t": args.trace_id},
            )
        ).all()
    if not spans:
        raise ToolError("No trace with this id in this project.")
    start = spans[0].start_time
    rows = [
        {"span": s.name, "parent": s.parent_span_id, "id": s.span_id, "offset_ms": round((s.start_time - start).total_seconds() * 1000),
         "duration_ms": round(s.duration_ms), "status": s.status, "error": (s.status_message or "")[:300] or None, "model": s.model, "tool": s.tool,
         "prompt_version": s.prompt_version, "tokens": (s.input_tokens or 0) + (s.output_tokens or 0),
         "attributes": {k: (v[:200] if isinstance(v, str) else v) for k, v in list((s.attributes or {}).items())[:12]}}
        for s in spans[:60]
    ]  # fmt: skip
    note = " (more than 60 spans; the first 60 are shown)" if len(spans) > 60 else ""
    return _result(ctx, f"{len(rows)} span(s){note}.", rows, f"trace {args.trace_id}", untrusted=True)


ID_LIKE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<id>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<value>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
]


def normalise_error(message: str) -> str:
    """'validation error: order_id '4417' ...' and '... '9920' ...' are the same problem."""
    for pattern, replacement in ID_LIKE:
        message = pattern.sub(replacement, message)
    return " ".join(message.split())[:200]


# Error spans read to group their messages. The true total is counted separately, so a capped scan says so.
ERROR_SCAN_LIMIT = 20_000


class ErrorsArgs(Window):
    tool: str | None = None
    model: str | None = None


@registry.tool(
    "group_errors",
    "Error messages in a range, grouped after replacing ids, numbers and quoted values, with counts and an example trace.",
)
async def group_errors(ctx: ToolContext, args: ErrorsArgs) -> ToolResult:
    _check_range(args.start, args.end)
    project = await _project(ctx, args.project)
    where, params = (
        ["project_id = :p", "start_time >= :s", "start_time < :e", "status = 'error'"],
        {"p": project.id, "s": args.start, "e": args.end},
    )
    for clause, name, value in (("tool = :tool", "tool", args.tool), ("model = :model", "model", args.model)):
        if value is not None:
            where.append(clause)
            params[name] = value
    sql_where = " AND ".join(where)
    async with tenant_session(ctx.org_id) as db:
        total = await db.scalar(text(f"SELECT count(*) FROM spans WHERE {sql_where}"), params)  # noqa: S608
        found = (
            await db.execute(
                text(
                    f"SELECT status_message, tool, model, trace_id FROM spans WHERE {sql_where} "  # noqa: S608
                    "ORDER BY start_time DESC, span_id LIMIT :limit"
                ),
                params | {"limit": ERROR_SCAN_LIMIT},
            )
        ).all()
    groups: dict[tuple, dict] = {}
    for r in found:
        key = (normalise_error(r.status_message or "(no message)"), r.tool, r.model)
        g = groups.setdefault(
            key, {"message": key[0], "tool": r.tool, "model": r.model, "count": 0, "example_trace": r.trace_id}
        )
        g["count"] += 1
    rows = sorted(groups.values(), key=lambda g: (-g["count"], g["message"]))[:50]
    complete = total <= len(found)
    summary = (
        f"{total} error span(s) in {len(groups)} group(s)."
        if complete
        else f"{total} error span(s) in total; the groups are counted over the most recent {len(found)} of them."
    )
    return _result(
        ctx,
        summary,
        rows,
        f"errors in {args.project}",
        extra={"error_spans": total, "scanned": len(found), "complete": complete},
        untrusted=True,
    )


@registry.tool(
    "list_changes",
    "What changed in a range: prompt releases, and the first appearance of each model, prompt version and service version "
    "(compared with the 14 days before the range).",
)
async def list_changes(ctx: ToolContext, args: Window) -> ToolResult:
    _check_range(args.start, args.end)
    project = await _project(ctx, args.project)
    rows: list[dict] = []
    async with tenant_session(ctx.org_id) as db:
        releases = (
            await db.execute(
                select(ReleaseRequest, Prompt.name).join(Prompt, Prompt.id == ReleaseRequest.prompt_id)
                .where(Prompt.project_id == project.id, ReleaseRequest.status == "approved", ReleaseRequest.decided_at >= args.start, ReleaseRequest.decided_at < args.end)
            )
        ).all()  # fmt: skip
        for r, name in releases:
            rows.append(
                {
                    "at": r.decided_at.isoformat(),
                    "change": "prompt release",
                    "what": f"{name}: version {r.from_version} -> {r.to_version}",
                }
            )
        for dimension in ("model", "prompt_version", "service_version"):
            seen = await db.execute(
                # A version number only means something within its prompt; a model or an application version
                # is new only if it was not seen at all, whichever prompt it ran under.
                text(f"""SELECT {dimension} AS value, {"prompt_name" if dimension == "prompt_version" else "''"} AS prompt_name,
                                min(bucket) AS first FROM rollups_5m
                         WHERE project_id = :p AND bucket >= :lookback AND bucket < :e AND CAST({dimension} AS text) NOT IN ('', '0')
                         GROUP BY 1, 2 HAVING min(bucket) >= :s"""),  # noqa: S608 - dimension from a fixed tuple
                {"p": project.id, "lookback": args.start - timedelta(days=14), "s": args.start, "e": args.end},
            )
            for v in seen:
                label = (
                    f"{v.prompt_name} version {v.value}"
                    if dimension == "prompt_version"
                    else f"{dimension.replace('_', ' ')} {v.value}"
                )
                rows.append(
                    {"at": v.first.isoformat(), "change": f"first seen: {dimension.replace('_', ' ')}", "what": label}
                )
    rows.sort(key=lambda r: r["at"])
    return _result(
        ctx,
        f"{len(rows)} change(s) found." if rows else "No changes found in this range.",
        rows,
        f"changes in {args.project}",
        untrusted=True,
    )


# ------------------------------------------------------------------ the one action


class RollbackArgs(BaseModel):
    project: str
    prompt: str
    to_version: int = Field(ge=1)
    rationale: str = Field(max_length=400)


@registry.tool(
    "propose_rollback",
    "Propose moving a prompt's live version back to an earlier version that was live before. An administrator must approve it.",
    level="needs_approval",
    idempotent=True,  # its only effect is database rows written together with the step record
)
async def propose_rollback(ctx: ToolContext, args: RollbackArgs) -> ToolResult:
    project = await _project(ctx, args.project)
    async with tenant_session(ctx.org_id) as db:
        prompt = (
            await db.execute(select(Prompt).where(Prompt.project_id == project.id, Prompt.name == args.prompt))
        ).scalar_one_or_none()
        if prompt is None:
            raise ToolError(f"There is no prompt '{args.prompt}' in {args.project}.")
        exists = (
            await db.execute(
                select(PromptVersion.id).where(
                    PromptVersion.prompt_id == prompt.id, PromptVersion.version == args.to_version
                )
            )
        ).first()
        was_live = (
            await db.execute(select(ReleaseRequest.id).where(ReleaseRequest.prompt_id == prompt.id, ReleaseRequest.to_version == args.to_version, ReleaseRequest.status == "approved").limit(1))
        ).first()  # fmt: skip
        approver = await db.scalar(
            text("SELECT decided_by FROM approvals WHERE run_id = :r AND step_idx = :i AND status = 'approved'"),
            {"r": ctx.run_id, "i": ctx.step_idx},
        )
    if exists is None or was_live is None:
        raise ToolError(
            f"Version {args.to_version} of {args.prompt} was never live, so it cannot be rolled back to. A new version must go through a release request."
        )
    if prompt.production_version == args.to_version:
        return ToolResult(f"Version {args.to_version} is already live; nothing was changed.")
    previous = prompt.production_version

    async def commit(db) -> None:
        release = ReleaseRequest(
            org_id=ctx.org_id, prompt_id=prompt.id, from_version=previous, to_version=args.to_version, status="approved",
            gate={"passed": True, "checks": [{"name": "rollback", "passed": True, "detail": f"Proposed by the Investigator: {args.rationale}"}]},
            requested_by=ctx.user_id, decided_by=approver, reason=args.rationale,
        )  # fmt: skip
        db.add(release)
        await db.flush()
        await db.execute(text("UPDATE release_requests SET decided_at = now() WHERE id = :id"), {"id": release.id})
        await db.execute(
            text("UPDATE prompts SET production_version = :v WHERE id = :p"), {"v": args.to_version, "p": prompt.id}
        )

    return ToolResult(
        f"Approved and done: {args.prompt} is now on version {args.to_version} (was {previous}).", commit=commit
    )


assert set(DIMENSIONS) >= {"model", "tool", "prompt_version"}
