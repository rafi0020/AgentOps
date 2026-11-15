"""Read load on the API: many signed-in people browsing the overview, trace lists and traces.

    cd backend && uv run locust -f load/locustfile.py --host http://localhost:3200 --headless \
        -u 1000 -r 100 -t 2m --csv load/results/read-u1000

Each simulated person signs in once (demo accounts), finds the projects and a page of traces from the last
day of the demo data, then requests pages with one to three seconds of think time. Start the stack with
docker-compose.load.yml so sign-in limits do not throttle the test itself.
"""

import random
from datetime import UTC, datetime, timedelta

from locust import FastHttpUser, between, task

ACCOUNTS = [
    ("viewer@lumen.demo", "Viewer#Demo2026"),
    ("engineer@lumen.demo", "Engineer#Demo2026"),
    ("admin@lumen.demo", "Admin#Demo2026"),
    ("viewer@orbit.demo", "Viewer#Demo2026"),
    ("engineer@orbit.demo", "Engineer#Demo2026"),
]
END = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)  # the demo data ends here
WINDOWS = [(END - timedelta(days=d), END) for d in (1, 1, 1, 7, 30)]


def window() -> dict:
    start, end = random.choice(WINDOWS)
    return {"from": start.isoformat(), "to": end.isoformat()}


class Reader(FastHttpUser):
    wait_time = between(1, 3)

    def on_start(self) -> None:
        email, password = random.choice(ACCOUNTS)
        response = self.client.post(
            "/api/auth/login", json={"email": email, "password": password}, name="POST /api/auth/login"
        )
        response.raise_for_status()
        self.projects = [p["id"] for p in self.client.get("/api/projects", name="GET /api/projects").json()["projects"]]
        self.traces: list[tuple[str, str]] = []
        if self.projects:
            project = random.choice(self.projects)
            listed = self.client.get(
                "/api/traces", params={"project": project, "from": (END - timedelta(days=1)).isoformat(), "to": END.isoformat()},
                name="GET /api/traces",
            )  # fmt: skip
            if listed.status_code == 200:
                self.traces = [(project, t["trace_id"]) for t in listed.json()["traces"]]

    @task(6)
    def overview(self) -> None:
        self.client.get("/api/metrics/overview", params=window(), name="GET /api/metrics/overview")

    @task(4)
    def trace_list(self) -> None:
        if self.projects:
            params = window() | {"project": random.choice(self.projects)}
            if random.random() < 0.3:
                params["status"] = "error"
            self.client.get("/api/traces", params=params, name="GET /api/traces")

    @task(4)
    def open_trace(self) -> None:
        if self.traces:
            project, trace = random.choice(self.traces)
            self.client.get(f"/api/traces/{project}/{trace}", name="GET /api/traces/{project}/{trace}")

    @task(2)
    def series(self) -> None:
        if self.projects:
            params = window() | {"project": random.choice(self.projects), "step": "1h", "group_by": "model"}
            self.client.get("/api/metrics/series", params=params, name="GET /api/metrics/series")

    @task(2)
    def me(self) -> None:
        self.client.get("/api/auth/me", name="GET /api/auth/me")

    @task(1)
    def alerts(self) -> None:
        self.client.get("/api/monitors/alerts?open_only=true", name="GET /api/monitors/alerts")

    @task(1)
    def investigations(self) -> None:
        self.client.get("/api/investigations", name="GET /api/investigations")
