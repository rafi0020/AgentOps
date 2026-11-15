"""Multiplies the generated demo traces inside PostgreSQL, to reach tens of millions of spans in minutes.

    DATABASE_URL_OWNER=...agentops_load uv run python -m load.amplify --copies 9

Writing 20 million spans through the application (the generator) takes hours on a laptop. For measuring
reads, the shape of the data matters, not how it arrived: each span and trace row is copied `copies` times
under a new trace id (md5 of the old id and the copy number), with the same times, names, statuses and
sizes. Roll-ups are not touched: they are per five-minute bucket and dimension, so their row count does not
grow with the number of spans, only their values would. Refuses to run twice.
"""

import argparse
import time

import psycopg

from app.api.health import libpq_url
from app.config import get_settings
from seed.demo_users import LUMEN, ORBIT
from seed.generate import APPS

DEMO = "project_id IN (SELECT id FROM projects WHERE name = ANY(%(apps)s))"  # not the ingestion test's project


def columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position", (table,)
    ).fetchall()
    return [r[0] for r in rows]


def main(args: argparse.Namespace) -> None:
    with psycopg.connect(libpq_url(get_settings().database_url_owner), autocommit=True) as conn:
        span_cols, trace_cols = columns(conn, "spans"), columns(conn, "traces")

        def copy_sql(table: str, cols: list[str], where: str) -> str:
            chosen = ", ".join("md5(trace_id || ':' || g)" if c == "trace_id" else c for c in cols)
            return (f"INSERT INTO {table} ({', '.join(cols)}) SELECT {chosen} FROM {table} "  # noqa: S608
                    f"CROSS JOIN generate_series(1, %(copies)s) g WHERE {where}")  # fmt: skip

        started = time.perf_counter()
        for org in (LUMEN, ORBIT):
            with conn.transaction():
                conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org),))
                spans, traces = conn.execute(
                    f"SELECT (SELECT count(*) FROM spans WHERE {DEMO}), (SELECT count(*) FROM traces WHERE {DEMO})",
                    {"apps": list(APPS)},
                ).fetchone()
                if spans > 2_000_000:
                    raise SystemExit(f"{spans:,} spans already: amplified before, refusing to copy copies")
                days = [
                    d
                    for (d,) in conn.execute(
                        f"SELECT DISTINCT start_day FROM spans WHERE {DEMO} ORDER BY 1", {"apps": list(APPS)}
                    ).fetchall()
                ]
            print(f"org {org}: {spans:,} spans, {traces:,} traces over {len(days)} days -> x{args.copies + 1}")
            for day in days:
                with conn.transaction():
                    conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org),))
                    conn.execute(
                        copy_sql("spans", span_cols, f"start_day = %(day)s AND {DEMO}"),
                        {"copies": args.copies, "day": day, "apps": list(APPS)},
                    )
                    conn.execute(
                        copy_sql(
                            "traces", trace_cols, f"start_time >= %(day)s AND start_time < %(day)s::date + 1 AND {DEMO}"
                        ),
                        {"copies": args.copies, "day": day, "apps": list(APPS)},
                    )
                print(f"  {day} done, {time.perf_counter() - started:.0f} s")
        conn.execute("ANALYZE spans")
        conn.execute("ANALYZE traces")
        print(f"finished in {time.perf_counter() - started:.0f} s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--copies", type=int, default=9)
    main(parser.parse_args())
