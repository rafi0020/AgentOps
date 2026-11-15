"""Every setting an operator is told about in .env.example reaches the containers.

Found by the final review: Compose substitutes only the variables docker-compose.yml names, so a setting written
in .env but missing there (ALLOW_PRIVATE_TARGETS, METRICS_TOKEN, the judge thresholds...) silently did nothing."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_documented_setting_is_passed_to_the_containers():
    names = re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.MULTILINE)
    compose = (ROOT / "docker-compose.yml").read_text()
    missing = [n for n in names if "${" + n not in compose]
    assert not missing, f"in .env.example but not passed through by docker-compose.yml: {missing}"
    for hardening in ("ALLOW_PRIVATE_TARGETS", "METRICS_TOKEN", "JUDGE_MIN_LABELS", "JUDGE_MIN_KAPPA",
                      "EXPERIMENT_MAX_COST_USD", "EXPERIMENT_MAX_ITEMS", "INGEST_REQUESTS_PER_MINUTE", "ORG_MAX_QUEUED_RUNS"):  # fmt: skip
        assert hardening in names, f"{hardening} is a setting operators need; document it in .env.example"


def test_every_published_port_is_bound_to_loopback_unless_it_is_a_documented_exception():
    """Nothing listens on the LAN by default. The web UI follows WEB_BIND (127.0.0.1 unless set). The second web
    mapping is the Docker bridge gateway, so containers of other projects (ProcureMind sends traces to
    host.docker.internal:3200) can reach ingestion; the bridge is not reachable from the network."""
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    allowed = {"127.0.0.1", "${WEB_BIND:-127.0.0.1}", "${WEB_INGEST_BIND:-172.17.0.1}"}
    published = [(name, str(p)) for name, svc in compose["services"].items() for p in svc.get("ports", [])]
    assert published
    for name, port in published:
        match = re.fullmatch(r"(.+):(\d+):(\d+)", port)  # host:published:container, the host part may hold ${VAR:-x}
        assert match, f"{name}: {port} names no host address, so it binds every interface"
        assert match.group(1) in allowed, f"{name}: {port} is published on {match.group(1)}, not 127.0.0.1"
    web = [p for n, p in published if n == "web"]
    assert "${WEB_BIND:-127.0.0.1}:3200:80" in web and "${WEB_INGEST_BIND:-172.17.0.1}:3200:80" in web
    assert re.search(r"^WEB_BIND=", (ROOT / ".env.example").read_text(), re.MULTILINE)
