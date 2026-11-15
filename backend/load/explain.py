"""Checks the query plans of the request-path queries at the current data size, and times them.

    DATABASE_URL=...agentops_load uv run python -m load.explain

No request-path query may read a large table from end to end: a sequential scan over more than 10,000 rows
fails the check and is named. Each query runs three times and the last time is reported (warm cache), next to
the first (cold-ish) one.
"""

import json
import sys
from datetime import timedelta

import psycopg

from app.api.health import libpq_url
from app.config import get_settings
from app.ingest.rollups import HIST_SUM
from app.worker.ingest import CLAIM
from seed.demo_users import LUMEN

BIG = 10_000
WHOLE_RANGE = {"overview, organisation, 30 days", "series by hour and model, 30 days"}
ORG = str(LUMEN)
ROLLUP = (
    "SELECT sum(calls), sum(errors), sum(roots), sum(input_tokens), sum(output_tokens), sum(cost_usd), "
    + HIST_SUM
    + " FROM rollups_5m WHERE bucket >= %(start)s AND bucket < %(end)s"
)
TRACES = "SELECT * FROM traces WHERE project_id = %(p)s AND start_time >= %(start)s AND start_time < %(end)s"
ORDER = " ORDER BY start_time DESC, trace_id DESC LIMIT 51"


def queries(project: str, trace: str, day_start, data_end) -> dict:
    day = {"p": project, "start": day_start, "end": data_end}
    month = {"p": project, "start": data_end - timedelta(days=30), "end": data_end}
    return {
        "trace list, one day": (TRACES + ORDER, day),
        "trace list, errors only": (TRACES + " AND status = 'error'" + ORDER, day),
        "trace list, by model": (TRACES + " AND %(m)s = ANY(models)" + ORDER, day | {"m": "model-large"}),
        "trace list, name search": (TRACES + " AND root_name ILIKE %(q)s" + ORDER, day | {"q": "%support%"}),
        "trace list, slow (>5 s), 30 days": (TRACES + " AND duration_ms >= 5000" + ORDER, month),
        "trace summary by id": ("SELECT * FROM traces WHERE project_id = %(p)s AND trace_id = %(t)s", {"p": project, "t": trace}),
        "spans of a trace": ("SELECT * FROM spans WHERE project_id = %(p)s AND trace_id = %(t)s ORDER BY start_time, span_id LIMIT 501",
                             {"p": project, "t": trace}),
        "overview, one project, one day": (ROLLUP + " AND project_id = %(p)s", day),
        "overview, organisation, 30 days": (ROLLUP, month),
        "series by hour and model, 30 days": (
            "SELECT date_bin('1 hour', bucket, TIMESTAMPTZ 'epoch') t, model, sum(calls), " + HIST_SUM + " "
            "FROM rollups_5m WHERE project_id = %(p)s AND bucket >= %(start)s AND bucket < %(end)s GROUP BY 1, 2", month),
        "error groups, one day (Investigator)": (
            "SELECT status_message, tool, model, trace_id FROM spans WHERE project_id = %(p)s AND status = 'error' "
            "AND start_time >= %(start)s AND start_time < %(end)s LIMIT 20000", day),
        "claim an ingest batch": (
            str(CLAIM).replace(":worker", "%(worker)s").replace(":lease", "%(lease)s"), {"worker": "explain", "lease": 30}),
    }  # fmt: skip


def seq_scans(node: dict, found: list) -> None:
    rows = node.get("Actual Rows", 0) * node.get("Actual Loops", 1) + node.get("Rows Removed by Filter", 0)
    if node.get("Node Type") == "Seq Scan" and rows > BIG:
        found.append(f"{node['Relation Name']} ({rows:,} rows read)")
    for child in node.get("Plans", []):
        seq_scans(child, found)


def main() -> int:
    failures, report = 0, []
    with psycopg.connect(libpq_url(get_settings().database_url_owner)) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
            spans = conn.execute("SELECT count(*) FROM spans").fetchone()[0]
            traces = conn.execute("SELECT count(*) FROM traces").fetchone()[0]
            project, data_end = conn.execute(
                "SELECT p.id, max(t.start_time) FROM projects p JOIN traces t ON t.project_id = p.id "
                "WHERE p.name = 'support-triage' GROUP BY 1"
            ).fetchone()
            trace = conn.execute(
                "SELECT trace_id FROM traces WHERE project_id = %s AND start_time < %s ORDER BY start_time DESC LIMIT 1",
                (project, data_end - timedelta(days=3)),
            ).fetchone()[0]
    print(f"spans {spans:,}, traces {traces:,} (all organisations visible to Lumen: Lumen's only)")
    with psycopg.connect(libpq_url(get_settings().database_url)) as conn:  # the application role: RLS applies
        for name, (sql, params) in queries(str(project), trace, data_end - timedelta(days=1), data_end).items():
            times = []
            for _ in range(3):
                with conn.transaction(force_rollback=True):  # the claim is an UPDATE; never keep its effect
                    conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
                    if name.startswith("claim"):
                        conn.execute("SELECT set_config('app.system', 'on', true)")
                    plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()[0][0]
                times.append(plan["Execution Time"])
            found: list[str] = []
            seq_scans(plan["Plan"], found)
            if name in WHOLE_RANGE:  # reads every row of the range by design; judged by its time, not its plan
                verdict = "reads the whole range" + (f" (sequential: {', '.join(found)})" if found else " (index)")
            else:
                verdict = "SEQUENTIAL SCAN on " + ", ".join(found) if found else "ok"
                failures += bool(found)
            print(f"  {name:<40}{times[0]:>9.2f} ms first{times[-1]:>9.2f} ms warm  {verdict}")
            report.append(
                {"query": name, "first_ms": round(times[0], 2), "warm_ms": round(times[-1], 2), "verdict": verdict}
            )
    if "--json" in sys.argv:
        print(json.dumps({"spans": spans, "traces": traces, "failures": failures, "queries": report}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
