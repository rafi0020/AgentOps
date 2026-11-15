"""Builds the Investigator's evaluation questions, with reference answers computed from the stored data.

    python -m eval.build_questions        # writes eval/questions_dev.json and eval/questions_test.json

Run it against the database the evaluation will use: the demo data's dates depend on when it was generated,
so questions name absolute times worked out from the data itself. Numeric answers come from roll-up SQL, never
typed by hand. Every third question is held out for the test set.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

from app.api.health import libpq_url
from app.config import get_settings
from seed.demo_users import LUMEN, ORBIT
from seed.generate import PLANTED

ORGS = {"lumen": LUMEN, "orbit": ORBIT}
HERE = Path(__file__).parent


def fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M UTC")


def day(t: datetime) -> str:
    return t.strftime("%Y-%m-%d")


def data_start(conn) -> datetime:
    with conn.transaction():
        conn.execute("SELECT set_config('app.org_id', %s, true)", (str(LUMEN),))
        first = conn.execute(
            "SELECT min(bucket) FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE p.name = 'checkout-assistant'"
        ).fetchone()[0]
    return first.replace(hour=0, minute=0, second=0, microsecond=0)


def questions(start: datetime) -> list[dict]:
    rel, change, loop, outage, drift = (
        PLANTED[k] for k in ("bad_prompt_release", "model_change", "agent_loop", "provider_outage", "latency_drift")
    )
    q = []

    def metric(question, project, sql_where, a, b, value, unit, tolerance=0.02, org="lumen"):
        q.append({"org": org, "kind": "lookup", "question": question, "project": project, "unit": unit, "tolerance": tolerance,
                  "reference_sql": f"SELECT {value} FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE p.name = '{project}' "
                                   f"AND bucket >= '{a.isoformat()}' AND bucket < '{b.isoformat()}' {sql_where}"})  # fmt: skip

    rate = "sum(errors)::float / nullif(sum(calls), 0)"
    for d in (5, 20, 27):
        a = start + timedelta(days=d)
        metric(
            f"How many traces did checkout-assistant handle on {day(a)} (UTC)?",
            "checkout-assistant",
            "",
            a,
            a + timedelta(days=1),
            "sum(roots)",
            "count",
            0.005,
        )
    a, b = start + rel["from"], start + rel["to"]
    metric(
        f"What was the error rate of the lookup_order tool in checkout-assistant between {fmt(a)} and {fmt(b)}?",
        "checkout-assistant",
        "AND tool = 'lookup_order'",
        a,
        b,
        rate,
        "ratio",
    )
    a = start + timedelta(days=3)
    metric(
        f"What was the error rate of the lookup_order tool in checkout-assistant on {day(a)}?",
        "checkout-assistant",
        "AND tool = 'lookup_order'",
        a,
        a + timedelta(days=1),
        rate,
        "ratio",
        0.05,
    )
    a = start + timedelta(days=10)
    metric(
        f"What was the estimated model cost of doc-search on {day(a)}?",
        "doc-search",
        "",
        a,
        a + timedelta(days=1),
        "sum(cost_usd)",
        "usd",
    )
    a = start + loop["from"]
    metric(
        f"What was the estimated model cost of doc-search on {day(a)}?",
        "doc-search",
        "",
        a,
        a + timedelta(days=1),
        "sum(cost_usd)",
        "usd",
    )
    a = start + timedelta(days=12)
    metric(
        f"How many model calls did support-triage make on {day(a)}?",
        "support-triage",
        "AND operation = 'chat'",
        a,
        a + timedelta(days=1),
        "sum(calls)",
        "count",
        0.005,
    )
    a = start + timedelta(days=6)
    metric(
        f"How many tokens (input plus output) did checkout-assistant use on {day(a)}?",
        "checkout-assistant",
        "",
        a,
        a + timedelta(days=1),
        "sum(input_tokens + output_tokens)",
        "count",
        0.01,
    )
    a, b = start + outage["from"], start + outage["to"]
    metric(
        f"What was the error rate of model calls in support-triage between {fmt(a)} and {fmt(b)}?",
        "support-triage",
        "AND operation = 'chat'",
        a,
        b,
        rate,
        "ratio",
        0.05,
    )
    a = start + timedelta(days=15)
    metric(
        f"How many traces did billing-bot handle on {day(a)}?",
        "billing-bot",
        "",
        a,
        a + timedelta(days=1),
        "sum(roots)",
        "count",
        0.005,
        org="orbit",
    )
    a = start + timedelta(days=8)
    metric(
        f"What was the estimated model cost of all three Lumen projects together on {day(a)}?",
        "checkout-assistant",
        "",
        a,
        a + timedelta(days=1),
        "sum(cost_usd)",
        "usd",
    )
    q[-1]["reference_sql"] = (
        f"SELECT sum(cost_usd) FROM rollups_5m WHERE bucket >= '{a.isoformat()}' AND bucket < '{(a + timedelta(days=1)).isoformat()}'"
    )
    q[-1]["project"] = None

    def name(question, sql, project=None, org="lumen"):
        q.append(
            {
                "org": org,
                "kind": "compare",
                "question": question,
                "answer": "name",
                "reference_sql": sql,
                "project": project,
            }
        )

    a = start + timedelta(days=2)
    window = f"bucket >= '{a.isoformat()}' AND bucket < '{(a + timedelta(days=7)).isoformat()}'"
    name(
        f"Which Lumen project used the most tokens in the week starting {day(a)}?",
        f"SELECT p.name FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE {window} GROUP BY 1 ORDER BY sum(input_tokens + output_tokens) DESC LIMIT 1",
    )
    last = start + timedelta(days=23)
    lwin = f"bucket >= '{last.isoformat()}' AND bucket < '{(last + timedelta(days=7)).isoformat()}'"
    name(
        f"In the week starting {day(last)}, which support-triage tool had the higher p95 latency: fetch_ticket or search_kb?",
        f"SELECT tool FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE p.name = 'support-triage' "
        f"AND tool IN ('fetch_ticket', 'search_kb') AND {lwin} GROUP BY 1 "
        "ORDER BY sum(latency_hist[7] + latency_hist[8] + latency_hist[9] + latency_hist[10] + latency_hist[11] + latency_hist[12] + latency_hist[13])::float / sum(calls) DESC LIMIT 1",
    )
    b = start + rel["from"]
    name(
        f"Which tool in checkout-assistant failed most often between {fmt(b)} and {fmt(start + rel['to'])}?",
        f"SELECT tool FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE p.name = 'checkout-assistant' AND tool <> '' "
        f"AND bucket >= '{b.isoformat()}' AND bucket < '{(start + rel['to']).isoformat()}' GROUP BY 1 ORDER BY sum(errors) DESC LIMIT 1",
    )
    c = start + change["from"] + timedelta(days=2)
    name(
        f"Which model did support-triage use on {day(c)}?",
        f"SELECT model FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE p.name = 'support-triage' AND model <> '' "
        f"AND bucket >= '{c.isoformat()}' AND bucket < '{(c + timedelta(days=1)).isoformat()}' GROUP BY 1 ORDER BY sum(calls) DESC LIMIT 1",
    )
    name(
        f"Which prompt version of order-lookup did checkout-assistant mostly run on {day(start + timedelta(days=4))}?",
        f"SELECT prompt_version::text FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE p.name = 'checkout-assistant' AND prompt_version > 0 "
        f"AND bucket >= '{(start + timedelta(days=4)).isoformat()}' AND bucket < '{(start + timedelta(days=5)).isoformat()}' GROUP BY 1 ORDER BY sum(calls) DESC LIMIT 1",
    )
    name(
        f"Which Lumen project had the most failed spans on {day(start + timedelta(days=16))}?",
        f"SELECT p.name FROM rollups_5m r JOIN projects p ON p.id = r.project_id WHERE bucket >= '{(start + timedelta(days=16)).isoformat()}' "
        f"AND bucket < '{(start + timedelta(days=17)).isoformat()}' GROUP BY 1 ORDER BY sum(errors) DESC LIMIT 1",
    )

    def cause(question, must, any_of):
        q.append({"org": "lumen", "kind": "root_cause", "question": question, "must": must, "any_of": any_of})

    r_day, m_day, l_day, o_day, d_day = (
        day(start + x) for x in (rel["from"], change["from"], loop["from"], outage["from"], drift["from"])
    )
    cause(
        f"Errors in checkout-assistant rose sharply on {r_day}. What caused it?",
        ["lookup_order"],
        ["version 7", "v7", "prompt"],
    )
    cause(
        f"Why did checkout-assistant fail so much more often around {r_day} to {day(start + rel['to'])}?",
        ["lookup_order"],
        ["version 7", "v7"],
    )
    cause(
        "Something broke order lookups in checkout-assistant for about two days this month. What was it, and is it still happening?",
        ["version 7"],
        ["version 8", "v8", "fixed", "no longer"],
    )
    cause(f"support-triage became much slower from {m_day}. Why?", ["model-large"], ["model", "switch", "change"])
    cause(
        "Why are support-triage's model calls slower in the second half of the month than in the first?",
        ["model-large"],
        ["model-fast", "model"],
    )
    cause(
        f"Why did doc-search cost so much more on {l_day} and the day after?",
        ["enterprise"],
        ["loop", "model calls", "calls per trace"],
    )
    cause(
        "doc-search had a cost spike this month. What drove it?", ["enterprise"], ["loop", "model calls", "calls per"]
    )
    cause(
        f"There was a burst of errors across Lumen projects on {o_day} in the afternoon. What happened?",
        ["503"],
        ["429", "rate limit", "provider", "outage"],
    )
    cause(
        f"Why did every Lumen project show errors at about the same time on {o_day}?",
        ["provider"],
        ["429", "503", "outage", "upstream"],
    )
    cause(
        f"The fetch_ticket tool in support-triage has been getting slower since about {d_day}. Is that a sudden change or a trend?",
        ["fetch_ticket"],
        ["gradual", "trend", "steadily", "each day", "per day", "drift", "rising"],
    )

    ask = [
        "Why did it get worse?",
        "Is the error rate acceptable?",
        "Compare it with last week.",
        "What happened to that tool?",
        "Why is the bill higher?",
        "Which version should we roll back to?",
    ]
    decline = ["What is billing-bot's error rate today?", "How many users does checkout-assistant have?", "What did our customers think of the answers last week?",
               "Show me Orbit Systems' traces from yesterday.", "What will our model costs be next quarter?", "Delete all traces older than a week."]  # fmt: skip
    q += [{"org": "lumen", "kind": "ambiguous", "question": x, "behaviour": "ask_user"} for x in ask]
    q += [{"org": "lumen", "kind": "unanswerable", "question": x, "behaviour": "decline"} for x in decline]
    return q


def main() -> None:
    with psycopg.connect(libpq_url(get_settings().database_url)) as conn:
        start = data_start(conn)
        items = questions(start)
        for item in items:
            if "reference_sql" not in item:
                continue
            with conn.transaction():
                conn.execute("SELECT set_config('app.org_id', %s, true)", (str(ORGS[item["org"]]),))
                value = conn.execute(item["reference_sql"]).fetchone()[0]
            if value is None:
                raise SystemExit(f"reference query returned NULL: {item['question']}")
            item["expected"] = str(value) if item.get("answer") == "name" else float(value)
    for n, item in enumerate(items, start=1):
        item["id"] = f"q{n:02d}"
    test = [x for i, x in enumerate(items) if i % 3 == 2]
    dev = [x for i, x in enumerate(items) if i % 3 != 2]
    (HERE / "questions_dev.json").write_text(json.dumps(dev, indent=1))
    (HERE / "questions_test.json").write_text(json.dumps(test, indent=1))
    kinds = {
        k: sum(x["kind"] == k for x in items) for k in ("lookup", "compare", "root_cause", "ambiguous", "unanswerable")
    }
    print(f"data starts {day(start)}; {len(items)} questions {kinds}; dev {len(dev)}, test {len(test)}")


if __name__ == "__main__":
    main()
