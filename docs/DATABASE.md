# Database

PostgreSQL 16 with the `pg_trgm` extension installed (see the note at the end). Migrations are in
`backend/alembic/versions/` and are the source of truth; the mapped classes in `backend/app/models/` mirror
them. Tables that the workers write with raw SQL (spans, roll-ups, scores) have no mapped class.

## Roles

| Role | Used by | Can do |
|---|---|---|
| `agentops_owner` | migrations (`alembic/env.py`) and the partition functions | Owns every table. Its rights are used only by `SECURITY DEFINER` functions and migrations. Row-level security is `FORCE`d, so the owner's ordinary sessions follow the same policies |
| `agentops_app` | API and workers | Reads and writes tables in `public` (default privileges from `infra/postgres/init.sh`). Not an owner, so it cannot create or drop tables or partitions. Cannot update or delete `audit_log`. Cannot read a span partition directly, only through `spans`. May execute the partition functions and the `sum_int_array` aggregate |

Both roles are created by `infra/postgres/init.sh` on an empty data directory. The databases are `agentops`
(running stack) and `agentops_test` (tests; `backend/tests/conftest.py` points at it).

There is no read-only role. The Investigator has no SQL tool, so nothing needs one.

## Tenancy

Each table holding an organisation's data has an `org_id` column and row-level security (`backend/app/rls.py`):

```sql
ALTER TABLE t ENABLE ROW LEVEL SECURITY;
ALTER TABLE t FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON t
  USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
```

`app_current_org()` reads the transaction-local setting `app.org_id`. `tenant_session(org_id)` in
`backend/app/db.py` sets it with `set_config(..., true)` at the start of each transaction, which is what works
behind PgBouncer in transaction mode. With no organisation set, a tenant table returns no rows.

Six tables also admit a *system* transaction (`app.system = on`, `system_session()`), whose policy reads
`org_id = app_current_org() OR app_is_system()`:

| Table | Why workers need it across organisations |
|---|---|
| `users` | Finding a person at sign-in |
| `projects` | Retention must know the longest period any project keeps, before removing a day |
| `ingest_keys` | Finding a key by its hash, before the organisation is known |
| `ingest_batches` | Ingest workers claim batches from any organisation |
| `runs` | Run workers claim runs from any organisation; budgets sum the day's spend |
| `experiments` | Experiment workers claim experiments from any organisation |

Three tables have no row-level security: `orgs` (the organisations themselves), `sessions` (keyed by the hash of
the cookie token and reached through a user, who belongs to one organisation) and `scheduled_jobs` (no tenant
data). Everything else holds organisation data and is protected.

## Tables

31 tables, plus one partition per day of `spans`. Column lists show the key columns, not every column.

### Identity and settings

| Table | Purpose | Key columns and notes |
|---|---|---|
| `orgs` | Organisations | `id`, `name` |
| `users` | People | `org_id`, `email` (unique across all organisations), `role` (`viewer`, `engineer`, `admin`), `password_hash` (scrypt), `is_active` |
| `sessions` | Server-side sessions | `user_id`, `token_hash` (SHA-256 of the cookie token; the token itself is never stored), `csrf_token`, `expires_at` |
| `audit_log` | Who did what | `org_id`, `user_id`, `action`, `target`, `detail` (JSON, masked), `ip`; append-only for the application |
| `llm_settings` | Model provider per organisation | `provider`, `model`, `base_url`, `api_key_enc` (encrypted with a key derived from `SECRET_KEY`), `fallback_model`, `price_input_per_mtok`, `price_output_per_mtok` |
| `prices` | Model prices used to estimate the cost of each span at ingestion | `(org_id, model)` primary key, `input_per_mtok`, `output_per_mtok` |

### Investigator runs

| Table | Purpose | Key columns and notes |
|---|---|---|
| `runs` | One question and how far it has got; also the work queue | `question`, `role`, `status` (`queued`, `running`, `waiting_approval`, `waiting_user`, `completed`, `failed`, `cancelled`, `budget_exceeded`), `result` (the report), `pending`, `steps_used`, `tokens_used`, `cost_usd`, `active_ms`, `cancel_requested`, `lease_owner`, `lease_expires_at`, `attempts` |
| `steps` | Every model call, tool call and runtime note | `run_id`, `idx`, `kind` (`model`, `tool`, `note`), `name`, `state` (`started`, `awaiting_approval`, `awaiting_user`, `done`), `call_id`, `input`, `output`, `error`, token and cost counters, `idempotency_key` (unique). Unique `(run_id, idx)` |
| `approvals` | Tool calls waiting for an administrator | `run_id`, `step_idx`, `tool`, `arguments`, `status` (`pending`, `approved`, `rejected`), `requested_by`, `decided_by`, `reason`. Unique `(run_id, step_idx)` |
| `evidence` | Stored results of tool calls that the report cites | `id` (the handle, such as `ev_7f3a91c2`), `run_id`, `kind` (`query`, `compute`, `chart`), `payload` (JSON), `row_count` |
| `scheduled_jobs` | Recurring jobs (retention, monitor evaluation) | `name` (primary key), `next_run_at`, `last_started_at`, `last_finished_at`, `last_error`. Not tenant data |

### Projects and ingestion

| Table | Purpose | Key columns and notes |
|---|---|---|
| `projects` | One per observed application | `org_id`, `name` (unique per organisation), `retention_days` (1 to 3650, default 30) |
| `ingest_keys` | Ingestion keys | `project_id`, `name`, `key_hash` (unique SHA-256; the plain key is shown once), `prefix`, `scopes` (`ingest`, `prompts:read`), `revoked_at`, `last_used_at` |
| `ingest_batches` | The inbox: received batches not yet stored | `project_id`, `payload` (zlib-compressed body), `content_type`, `span_count`, `status` (`pending`, `processing`, `dead`), `attempts`, `lease_owner`, `lease_expires_at`, `error`, `received_at` |

### Spans, traces and roll-ups

| Table | Purpose | Key columns and notes |
|---|---|---|
| `spans` | One row per span, partitioned by day on `start_day` | Primary key `(project_id, start_day, trace_id, span_id)`. Columns: `parent_span_id`, `name`, `kind`, `start_time`, `end_time`, `duration_ms`, `status`, `status_message`, `operation`, `model`, `tool`, `provider`, `prompt_name`, `prompt_version`, `service_name`, `service_version`, `session_id`, `input_tokens`, `output_tokens`, `cost_usd`, `attributes` (JSON), `content` (JSON, when sent) |
| `spans_YYYYMMDD` | One partition per day, created by `ensure_span_partition` | Not granted to `agentops_app` |
| `spans_default` | Spans whose day has no partition | Same, for spans far outside the window of partitions |
| `traces` | One summary per trace, recomputed from its spans on every batch | `(project_id, trace_id)` primary key; `root_name`, `status` (`error` if any span errored), `start_time`, `end_time`, `duration_ms`, `span_count`, `error_count`, token and cost totals, `models`, `tools` (arrays), `session_id`, `service_name`, `service_version` |
| `rollups_5m` | Figures per project and five-minute bucket, per combination of model, operation, tool, prompt name and version, and application version | `calls`, `errors`, `roots`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_hist` (an `int[]` histogram, 13 slots). Primary key on the bucket and the six dimensions |
| `score_rollups_5m` | Online score counts and sums per project, bucket and evaluator | `count`, `sum`. Primary key `(project_id, bucket, evaluator_id)` |

### Evaluation

| Table | Purpose | Key columns and notes |
|---|---|---|
| `datasets` | Named sets of items | `project_id`, `name` (unique per project), `description` |
| `dataset_items` | Items of a dataset | `dataset_id`, `seq` (identity; keeps insertion order), `input` (JSON), `expected` (JSON), `metadata`, `source_trace_id` |
| `evaluators` | Named scorers | `kind` (`exact`, `contains`, `regex`, `numeric`, `json_schema`, `latency_under`, `cost_under`, `judge`), `config` (JSON; a judge's rubric is here) |
| `experiments` | One run of a target over a dataset | `dataset_id`, `target` (JSON: a prompt version or an HTTP target), `evaluator_ids`, `status` (`queued`, `running`, `completed`, `failed`, `cancelled`), `totals` (per evaluator: mean, pass rate, invalid count), `cost_usd`, `items_done`, `items_total`, lease columns |
| `experiment_results` | One result per experiment and item | `output`, `output_hash` (of the output the judge saw), `latency_ms`, `cost_usd`, `error`, `scores` (JSON, keyed by evaluator). Unique `(experiment_id, item_id)` |
| `human_labels` | Human grades of an output, used to calibrate a judge | `evaluator_id`, `item_id`, `output_hash`, `score` (1 to 5), `labelled_by`. Unique on evaluator, item, output hash and labeller |
| `prompts` | A named prompt in a project | `primary_evaluator_id`, `min_score`, `max_drop` (default 0.02), `production_version` (the live version) |
| `prompt_versions` | Immutable numbered versions of a prompt | `version`, `text`, `variables`, `model_settings`, `note`. Unique `(prompt_id, version)` |
| `release_requests` | Requests to make a version live | `from_version`, `to_version`, `experiment_id`, `gate` (JSON: each check and whether it passed), `status` (`pending`, `approved`, `rejected`, `blocked`), `requested_by`, `decided_by` |

### Monitors

| Table | Purpose | Key columns and notes |
|---|---|---|
| `monitor_rules` | A metric compared with a threshold over a window | `metric` (`error_rate`, `p95_latency_ms`, `cost_per_hour`, `score_avg`), `evaluator_id`, `filter` (JSON), `comparison` (`<` or `>`), `threshold`, `window_minutes`, `min_calls` (default 20), `status` (`active`, `paused`) |
| `monitor_alerts` | What a rule raised | `window_start`, `window_end`, `value`, `calls`, `acknowledged_by`, `investigation_run_id`. Unique `(rule_id, window_start)` |
| `online_evaluators` | Deterministic evaluators applied to live traffic | `(project_id, evaluator_id)` primary key, `sample_rate` (0 to 1) |
| `trace_scores` | One online score per span and evaluator | `trace_id`, `span_id`, `value`, `passed`, `reason`. Primary key `(project_id, trace_id, span_id, evaluator_id)`, so a re-ingested batch adds nothing |

## Partitioning of spans

`spans` is declared `PARTITION BY RANGE (start_day)`. One partition holds one day and is named
`spans_YYYYMMDD`. The reasons are in the design section of `ARCHITECTURE.md`; in short, a retention job removes
a whole day with one `DROP TABLE`, and a query for a time range touches only the days it needs.

Partition management is done by three functions, created in migrations `0011` and `0012`. They are the only
DDL the application can run, and they run with the owner's rights (`SECURITY DEFINER`, `search_path = public`):

| Function | Does |
|---|---|
| `ensure_span_partition(day date) → boolean` | Creates `spans_YYYYMMDD` for that day if it does not exist, then revokes all rights on it from `agentops_app`. Returns `false` when another worker made it first, or when rows for that day are already in `spans_default` |
| `drop_span_partition(day date) → boolean` | Drops that day's partition |
| `span_partition_days() → setof date` | Lists the days that have their own partition |

Revoking rights on each partition is what makes row-level security hold: the application reads and writes
spans only through the parent table, where the policy applies. `test_partitions_cannot_be_read_around_the_parent_table`
checks this. `ensure_partitions()` (`backend/app/ingest/partitions.py`) creates partitions from 35 days before
today to 2 days ahead; it runs when an ingest worker starts and then hourly. A span dated outside that window
goes to `spans_default` and is removed by row-level trimming, not by dropping a partition.

## Roll-ups and the histogram aggregate

`rollups_5m` is updated in the same transaction as the spans, and only from the spans that were new
(`backend/app/ingest/rollups.py`). Each row is one bucket and one combination of dimensions; the update adds to the
counters and merges the latency histograms.

Latency is kept as a histogram with 12 fixed bounds in milliseconds (`LATENCY_BOUNDS_MS`: 10, 25, 50, 100, 250,
500, 1000, 2500, 5000, 10000, 30000, 60000), so 13 slots. Histograms can be added together and percentiles
cannot, which is why this form is stored. Two SQL objects make the addition possible in the database:

- `int_array_add(int[], int[]) → int[]`: element-wise sum of two arrays, treating missing elements as zero.
- `sum_int_array(int[])`: an aggregate built from it, so `sum_int_array(latency_hist)` gives the histogram of many
  buckets.

`query_rollups()` does not call `sum_int_array` on the read path. It sums each of the 13 slots with `sum()` in one
`SELECT` (the `HIST_SUM` expression in `rollups.py`). The code comment records that this was faster on a month of one
organisation's roll-ups. Nothing calls `sum_int_array` now; it stays defined (migration `0012`), and removing it would
be a separate migration. The ingest path uses `int_array_add` directly, in the upsert.

A percentile is the upper bound of the slot that holds the rank. Calls above 60 seconds are reported as 120000 ms.
Percentiles are therefore approximate to the width of a bucket, and the API says so.

The primary keys of `rollups_5m` and `score_rollups_5m` lead with `project_id`, which suits per-project reads. A read
for a whole organisation (the Overview with no project chosen) needs the organisation-and-bucket indexes added in
migration `0015`, otherwise it reads every organisation's rows and keeps the rows of one. The test
`tests/test_rollups.py::test_an_organisation_wide_read_does_not_scan_other_organisations_rollups` is the regression check.

## Retention

Each project keeps its data for `retention_days` (default 30). `apply_retention()` (`backend/app/ingest/retention.py`)
runs hourly in the worker:

1. It computes the earliest day any project, in any organisation, still keeps: the minimum of today minus each
   project's `retention_days` (30 days back when there are no projects).
2. It drops every day partition before that day, with `drop_span_partition`. This is instant and leaves no bloat.
3. For each organisation, in that organisation's session, it deletes for each project the spans, trace summaries,
   roll-ups and scores older than that project's cutoff. Days that another project still needs are trimmed row by
   row, so a project with shorter retention does not keep another project's expired data.

`test_retention.py::test_partitions_older_than_every_projects_retention_are_dropped` and
`::test_old_data_goes_and_a_project_with_longer_retention_keeps_its_own` cover both halves.

## Indexes worth knowing

| Index | Serves |
|---|---|
| `runs_claim_idx` on `runs (created_at)`, partial (queued or running) | The run worker's claim query |
| `runs_org_created_idx`, `runs_org_active_idx` (partial) | Run lists, the per-organisation limits, daily spend |
| `rollups_5m_org_bucket_idx (org_id, bucket)`, `score_rollups_5m_org_bucket_idx (org_id, bucket)` | Organisation-wide figures and score averages (migration 0015) |
| `steps (run_id, idx)` unique, `steps.idempotency_key` unique | Loading a run's steps in order; preventing a step being recorded twice |
| `approvals_org_status_idx` | Pending approvals |
| `spans_trace_idx (project_id, trace_id)` | Reading one trace |
| `spans_time_idx (project_id, start_time)` | Time-range queries |
| `spans_error_idx (project_id, start_time)`, partial on `status = 'error'` | Error queries |
| `traces_time_idx (project_id, start_time DESC, trace_id)` | The trace list and its keyset cursor |
| `traces_error_idx`, `traces_session_idx` (partial) | Errors only; sessions |
| `ingest_batches_claim_idx (received_at)`, partial (not dead) | The ingest worker's claim |
| `ingest_batches_dead_idx (org_id, received_at)`, partial (dead) | The dead-batch list |
| `ingest_keys_project_idx`, `ingest_keys.key_hash` unique | Key listing; key lookup at each request |
| `experiments_claim_idx` (partial, queued or running) | The experiment worker's claim |
| `dataset_items_dataset_idx (dataset_id, seq)` | Items in order |
| `experiment_results_exp_idx (experiment_id)` | An experiment's results |
| `release_requests_prompt_idx (prompt_id, created_at DESC)` | A prompt's release history |
| `monitor_alerts_org_idx (org_id, created_at DESC)` | Alert lists |
| `audit_org_time_idx (org_id, created_at DESC)` | The audit log |

## Migrations

| Revision | File | Adds |
|---|---|---|
| 0001 | `0001_identity.py` | `app_current_org()` and `app_is_system()`; `orgs`, `users`, `sessions`, `audit_log` |
| 0003 | `0003_results.py` | `evidence` |
| 0004 | `0004_llm_settings.py` | `llm_settings` |
| 0005 | `0005_runs.py` | `runs`, `steps`, `approvals`; the foreign key from `evidence` to `runs` |
| 0006 | `0006_scheduler.py` | `scheduled_jobs` |
| 0010 | `0010_projects.py` | `projects`, `ingest_keys`, `ingest_batches` |
| 0011 | `0011_spans.py` | `spans` (partitioned), `spans_default`, `ensure_span_partition` and `drop_span_partition`, `traces`, `prices` |
| 0012 | `0012_rollups.py` | `int_array_add`, `sum_int_array`, `rollups_5m`, `score_rollups_5m`, `span_partition_days()` |
| 0013 | `0013_evals.py` | `datasets`, `dataset_items`, `evaluators`, `prompts`, `prompt_versions`, `experiments`, `experiment_results`, `human_labels`, `release_requests` |
| 0014 | `0014_monitors.py` | `monitor_rules`, `monitor_alerts`, `online_evaluators`, `trace_scores` |
| 0015 | `0015_rollup_org_index.py` | Indexes `rollups_5m_org_bucket_idx` and `score_rollups_5m_org_bucket_idx` for organisation-wide reads |

The numbering has gaps (0002 and 0007 to 0009 are not used). Revisions 0001 and 0003 to 0006 are copied from the
FinOps project and keep its numbers, so the copied files did not need renaming; new migrations start at 0010.

### Running them

Migrations run as `agentops_owner` directly against PostgreSQL, not through PgBouncer. `alembic/env.py` takes an
advisory lock (724101), so containers that start together apply them one at a time.

- With Docker Compose, the `migrate` service runs `python -m app.bootstrap` before `api`, `worker` and `ingest`
  start. It applies migrations to `head` and, when `SEED_DEMO=true`, loads the demo accounts and data.
- By hand, from `backend/`, with the owner's connection string in the environment:

```bash
DATABASE_URL_OWNER=postgresql+psycopg://agentops_owner:<password>@localhost:55442/agentops \
  uv run alembic upgrade head
```

Tests use `agentops_test`, which `make test` expects after `make dev-up` has started the database.

## Notes

- **`pg_trgm` is installed but not used.** `infra/postgres/init.sh` creates the extension, and FinOps used it for
  searching saved investigations. AgentOps has no trigram index, so the extension can be removed from `init.sh`
  without changing behaviour.
- **Prices are applied on arrival.** A span's cost is computed from the `prices` table when the span is ingested
  and stored with it. Changing a price changes only spans that arrive afterwards. A model with no price gets no
  cost (`NULL`), which is not the same as zero.
- **Scale.** Row counts, sizes and query timings at 1.4 and 17 million spans are in SCALE.md.
