"""Service time per request with no concurrency: one signed-in person, each endpoint 40 times in a row.

    uv run python -m load.latency --host http://localhost:3200

Load tests measure how latency grows under contention; this measures what one request costs at the current
data size, which does not depend on what else the machine is doing as much.
"""

import argparse
import random
import statistics
import time
from datetime import UTC, datetime, timedelta

import httpx


def main(args: argparse.Namespace) -> None:
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day = {"from": (end - timedelta(days=1)).isoformat(), "to": end.isoformat()}
    month = {"from": (end - timedelta(days=30)).isoformat(), "to": end.isoformat()}
    with httpx.Client(base_url=args.host, timeout=60) as c:
        c.post(
            "/api/auth/login", json={"email": "engineer@lumen.demo", "password": "Engineer#Demo2026"}
        ).raise_for_status()
        projects = {p["name"]: p["id"] for p in c.get("/api/projects").json()["projects"]}
        p = projects["support-triage"]
        traces = [
            t["trace_id"] for t in c.get("/api/traces", params=day | {"project": p, "limit": 100}).json()["traces"]
        ]
        cases = {
            "trace list, one day": lambda: c.get("/api/traces", params=day | {"project": p}),
            "trace list, failures, 30 days": lambda: c.get("/api/traces", params=month | {"project": p, "status": "error"}),
            "trace page": lambda: c.get(f"/api/traces/{p}/{random.choice(traces)}"),
            "overview, one day (cached 30 s)": lambda: c.get("/api/metrics/overview", params=day | {"project": p}),
            "series by hour, 30 days (cached 30 s)": lambda: c.get(
                "/api/metrics/series", params=month | {"project": p, "step": "1h", "group_by": "model"}),
            "who am I": lambda: c.get("/api/auth/me"),
        }  # fmt: skip
        for name, call in cases.items():
            times = []
            for _ in range(args.n):
                t = time.perf_counter()
                call().raise_for_status()
                times.append((time.perf_counter() - t) * 1000)
            times.sort()
            print(f"  {name:<38} p50 {statistics.median(times):7.1f} ms   p95 {times[int(len(times) * 0.95)]:7.1f} ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="http://localhost:3200")
    parser.add_argument("--n", type=int, default=40)
    main(parser.parse_args())
