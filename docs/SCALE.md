# Scale: what was measured

Everything here was measured on one laptop on 2026-11-14, with the load generator on the same machine as the
system under test. It shows how the system behaves and where it slows down on this hardware. It is not a
capacity claim for a production deployment.

**Machine:** 16 logical cores (Intel i9-11900H), 31 GB RAM, Linux, Docker 29. PostgreSQL 16 (`shared_buffers`
512 MB), PgBouncer in transaction mode (pool of 40), Redis 7, nginx. Four API containers, one uvicorn process
each.

**Read this first: the machine was shared.** From 06:36 UTC, an unrelated workload on the same machine
(another project's container and two other processes) used 6–9 of the 16 cores for the rest of the session.
It was not mine to stop. The first ingestion run started 50 seconds before it; every other measurement here
ran beside it. The figures below are therefore lower bounds, and the read-load figures in particular are not
comparable with FinOps Agent's, which were taken on a quiet machine. Where a number is affected, it says so.

**Data:** measured in a separate database (`agentops_load`) so the demo database used by the evaluation was
not touched (`backend/load/create_load_db.sh`, `docker-compose.load.yml`). The demo generator wrote 1.84 million
spans (380,000 traces, 30 days, two organisations) through the application's own write path in 6 minutes,
about 5,100 spans a second. For the larger size, `backend/load/amplify.py` copied every demo trace nine times
inside PostgreSQL under new trace ids (same times, names and sizes): 18.4 million demo spans and 3.8 million
traces. The ingestion tests added another 2.95 million spans in a project of their own. At the large size the
span partitions take 9.2 GB (39 day partitions) and the traces table 2.2 GB.

## 1. Ingestion

`backend/load/ingest_load.py`: 8 concurrent senders, each posting gzip-compressed protobuf batches of 200 new
spans (fresh trace and span ids, so the idempotent write cannot skip duplicates) for 60 seconds. The inbox depth
was read from the database every two seconds, and sampling continued until every accepted span was written, so
"written" is end to end: accepted spans divided by the time until the inbox was empty.

| Ingest containers (4 batches at once each) | Accepted | Written | Inbox at its deepest | Inbox empty after senders stopped | Request p50 / p95 | Rejected |
|---|---|---|---|---|---|---|
| 1 | 20,200 spans/s | 10,400 spans/s | 4,236 batches (847,000 spans) | 56 s | 55 / 107 ms | 0 |
| 4 | 29,000 spans/s | 25,700 spans/s | 871 batches | 7.6 s | 38 / 65 ms | 0 |

What this says:

- **When senders outrun the workers, the endpoint keeps accepting.** With one ingest container the inbox grew
  steadily (746 batches after 12 s, 2,053 after 38 s, 4,125 after 62 s) while every request was still answered
  in about 55 ms, because acknowledging only needs one row written to the inbox. The test stopped sending at
  60 s; the backlog was then written in 56 s. During that time traces, roll-ups, charts and monitors lagged the
  traffic by up to about a minute and a half. Nothing was lost or refused.
- **Writing scales with ingest containers**: 2.5× from one to four. Four containers kept up with what eight
  senders could offer; the backlog never exceeded 15 seconds of traffic.
- **The accepted rate was limited by the test, not the endpoint**: the senders build every batch in Python on
  the same machine. The highest acceptance rate was not found.
- Both runs shared the machine with the other workload (the first for all but 50 s of it).

The default limit of 600 requests per minute per ingestion key (2,000 spans/s at 200-span batches) was lifted
for this test; it protects the platform from one runaway application, and is a setting.

## 2. Query time against data size

`python -m load.explain` runs each request-path query three times with `EXPLAIN ANALYZE`, as the application
role (so row-level security applies), and fails if one reads more than 10,000 rows from a table end to end.
Figures are warm (third run); "first" is in the result files `backend/load/results/explain-*.txt`.

| Query | 1.4 M spans in the organisation | 17 M spans |
|---|---|---|
| Trace list, one day | 0.4 ms | 0.5 ms |
| Trace list, failures only | 0.1 ms | 0.2 ms |
| Trace list, by model | 0.2 ms | 0.3 ms |
| Trace list, slow traces over 30 days | 0.5 ms | 1.2 ms |
| Trace list, name contains (one day) | 5 ms | 66 ms |
| Trace summary by id | 0.1 ms | 0.2 ms |
| Spans of one trace (all day partitions) | 1.7 ms | 4.7 ms |
| Overview, one project, one day | 1.9 ms | 2.4 ms |
| Overview, whole organisation, 30 days | 139 ms | 178 ms |
| Series by hour and model, 30 days | 53 ms | 104 ms |
| Error groups for one day (the Investigator's `group_errors`) | 1.2 ms | 3.1 ms |
| Claim an ingest batch | 0.1 ms | 0.3 ms |

What this says:

- **Reading traces does not slow down with volume**: every trace-path query uses an index bounded by project and
  time, and stays in single-digit milliseconds at 17 million spans.
- **Charts read roll-ups, not spans.** The roll-up table has the same number of rows (107,000 for 30 days of one
  organisation) at both sizes, because it holds one row per five-minute bucket and combination of dimensions.
  The 30-day organisation overview reads all of them by design and is cached for 30 s.
- **The name search is the exception.** It filters with `ILIKE`, which is checked on every trace of the project
  in the range. For one day that is 66 ms; for 31 days with no match it took **4.1 s** at about 1.15 million
  traces in the project. A trigram index does not help here: under row-level security PostgreSQL only lets
  leakproof operators use an index, and `ILIKE` is not one. The fix would be a small table of each project's
  distinct trace names, searched first, then traces fetched by `root_name = ANY(...)` (equality is leakproof).
  Not done.

### Defects the measurement found, fixed

- **Latency histograms were added up with a slow aggregate.** `sum_int_array`, a SQL function, built a new array
  for every row. The 30-day organisation overview took 0.86 s and the 30-day series 0.40 s at 1.4 million spans.
  The histogram has a fixed number of slots, so `query_rollups` now sums each slot with the built-in `sum()`:
  0.14 s and 0.05 s, and the one-day overview went from 15 ms to 1.4 ms. Guarded by
  `test_histograms_read_back_equal_the_stored_rows_merged`.
- **An organisation-wide read scanned every organisation's roll-ups.** The roll-up tables were indexed by project
  only, so the Overview with no project chosen read all organisations' rows and kept its own. Migration 0015 adds
  an index on organisation and time (`test_an_organisation_wide_read_does_not_scan_other_organisations_rollups`).
  With two demo organisations the difference is small; with thousands it is the difference between reading one
  organisation's rows and reading everyone's.

## 3. Service time per request

`python -m load.latency`: one signed-in person, each endpoint 40 times in a row, at 17 million spans. This is
what one request costs, through nginx and the API, with no concurrency (on the shared machine).

| Endpoint | p50 | p95 |
|---|---|---|
| Trace list, one day | 35 ms | 53 ms |
| Trace list, failures, 30 days | 37 ms | 47 ms |
| Trace page (spans, timeline data) | 54 ms | 112 ms |
| Overview, one day (cached for 30 s) | 16 ms | 25 ms |
| Series by hour and model, 30 days (cached) | 35 ms | 49 ms |
| Who am I (session check) | 13 ms | 21 ms |

The database part of each of these is a few milliseconds (section 2); the rest is the API process: session
check, serialising the response, and Python on a contended core.

## 4. Reading under load: people browsing

Locust, `backend/load/locustfile.py`: each simulated person signs in, then requests the overview, trace lists,
traces, series, alerts and investigations with 1–3 s between requests. Stats are reset when the last person has
started (`--reset-stats`).

| When | Data | People | Requests/s | p50 | p95 | Errors |
|---|---|---|---|---|---|---|
| 06:38, the other workload just starting | 4.8 M spans | 1,000 | 444 | 5 ms | not kept* | 0 (apart from the defect below) |
| 07:25, beside the other workload | 21 M spans | 1,000 | 142 | 1.6 s | 24 s | 0.14% |
| 07:28, beside the other workload | 21 M spans | 3,000 | 154 | 7.6 s | 56 s | 4% (timeouts) |

\* The first run's figures include the sign-in ramp (it ran without `--reset-stats`), and its result files were
overwritten by the re-run after the fix below; only its summary line (requests/s, median, errors) survives.

What this says, honestly:

- **On a mostly free machine, 1,000 people were served with a median of 5 ms**, at the same request rate
  FinOps reached (444 against 461 requests/s).
- **Beside the other workload, the four API processes were CPU-bound** at about 35 requests/s each, every one at
  ~95% of a core. Requests queued, database connections were held while waiting, and at 3,000 people the
  connection pool (10 + 10 per process) ran out and requests timed out. These runs measure the shared machine as
  much as the code. **Read capacity on a quiet machine at 20 million spans was not measured.**
- Sign-in is deliberately expensive (scrypt). For the load runs the demo accounts were re-hashed with a cheaper
  setting, as in FinOps, so that a burst of sign-ins does not dominate; the one-off burst at the start of a run
  is still where latency is worst.

### A defect the load test found

Viewers could open the Investigations screen from their menu, but its list returned 403 (1,052 failures in
the first run). Viewers can read every investigation by its id, so they may now list them too; they still
cannot start, answer or cancel one (`test_viewers_cannot_start_runs_but_can_read_them`).

## 5. Live connections

`backend/load/sse_connections.py`: 2,000 browsers following one investigation over Server-Sent Events for 45 s,
through nginx, on four API containers. The investigation was parked waiting for an approval, so every
connection stayed open on keep-alives.

| | |
|---|---|
| Connections established | 2,000 of 2,000 |
| Dropped before the end | 0 |
| Time to first byte | p50 14 s, p95 47 s (see below) |
| API memory while held | 220–260 MB per container |

The script opens at most 200 connections at a time, so its time to first byte includes waiting for earlier
waves; on the contended machine each wave took seconds. FinOps, with the same design on a quiet machine,
measured p50 1.8 s. What this run does show is that the connections are held and nothing is dropped. Each open
connection polls its run's event stream in Redis every 0.4 s (`backend/app/api/events.py`), which keeps Redis
connections few at the cost of some CPU per connection.

## Not tested

- Read throughput on a quiet machine at 20 million spans (see section 4), and ingestion on a quiet machine.
- Any load with a real model: an investigation takes 10–60 s, almost all of it waiting for the model, so the
  number of concurrent investigations is set by the provider's rate limits and `WORKER_CONCURRENCY`.
- More than one machine; a managed database; network latency between tiers.
- Many organisations at once (two were used). The roll-up index above was added because of what the plans showed,
  not because many organisations were measured.
- Sustained ingestion for hours, retention dropping partitions under load, memory growth over time.
- Spans beyond about 21 million. PostgreSQL with day partitions was chosen to keep one store and one tenancy
  model; at hundreds of millions of spans a column store (ClickHouse and similar) would be the next step.

## How to repeat

```bash
sh backend/load/create_load_db.sh
cd backend
export DATABASE_URL=postgresql+psycopg://agentops_app:agentops_app@localhost:56442/agentops_load
export DATABASE_URL_OWNER=postgresql+psycopg://agentops_owner:agentops_owner@localhost:55442/agentops_load
uv run alembic upgrade head && uv run python -m seed.generate --scale 1
cd .. && docker compose -f docker-compose.yml -f docker-compose.load.yml up -d --build --scale api=4 --scale ingest=1
cd backend
uv run python -m load.explain
uv run python -m load.ingest_load --seconds 60 --label ingest1     # then --scale ingest=4 and --label ingest4
uv run locust -f load/locustfile.py --host http://localhost:3200 --headless -u 1000 -r 40 -t 175s --reset-stats
uv run python -m load.sse_connections --connections 2000 --hold 45
uv run python -m load.amplify --copies 9 && uv run python -m load.explain && uv run python -m load.latency
```

Afterwards, `docker compose up -d` (without the load file) points the stack back at the demo database.
