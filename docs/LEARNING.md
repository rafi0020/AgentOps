# How AgentOps works: a guided read

This is a reading order for the code, for someone who wants to understand how an observability and evaluation
platform for LLM applications is built, and how an agent inside it is built. Each stop names the file and function
to open, what to notice there, and the test that proves the behaviour. Read them in order; it takes about two
hours.

Nothing here uses an agent framework. The agent loop, the tool registry, the durable state and the recovery logic
are in `backend/app/agent/` and `backend/app/worker/run.py`, about 900 lines, written to be read. The Investigator
is an `AgentSpec` (instructions and tools) handed to that loop.

Tests are in `backend/tests/`. Run one with `cd backend && uv run pytest tests/test_runtime.py -q`. Tests that call a
real model are marked `live` and need the gateway in `../.env.local`; skip them with `RUN_LIVE=0`.

## Part A: ingestion

### 1. Receiving a batch

Open `backend/app/api/ingest.py`, `receive_traces()`, then `backend/app/ingest/otlp.py`, `parse_request()`.

Notice that the response is sent only after `store_batch()` has committed (the write happens before the `return`).
A batch that parses to zero spans is answered `200` and not stored. Size is checked twice: the declared length, and
the length after gunzip, which is read with a ceiling so that a small body cannot expand without bound
(`_inflate`). JSON is walked by hand because OTLP's JSON form writes ids as hex, which the generic protobuf JSON
parser would misread as base64.

Tests: `tests/test_ingest_endpoint.py::test_if_the_batch_cannot_be_stored_the_sender_is_told_to_retry`,
`::test_oversized_and_decompression_bomb_bodies_are_refused`, `::test_requests_over_the_per_key_limit_get_429`.

### 2. The inbox and its claim

Open `backend/app/ingest/inbox.py`, `store_batch()`, and `backend/app/worker/ingest.py`, the `CLAIM` statement and
`claim_batch()`.

The inbox row holds the compressed body as it arrived. The claim is an `UPDATE ... FROM` a `SELECT ... FOR UPDATE SKIP
LOCKED`: it sets a 30-second lease and increments `attempts`. A row whose lease has expired is claimable again, which
is how a crashed worker's batch is recovered. Notice `handle()`: a failure leaves the batch `pending` with a growing
delay (`2 × attempts` seconds), and after `MAX_ATTEMPTS` (5) it is kept as `dead` for an administrator.

Test: `tests/test_ingest_worker.py::test_a_batch_that_keeps_failing_is_kept_as_dead_and_can_be_replayed`.

### 3. One transaction per batch

Still in `backend/app/worker/ingest.py`: `process_batch()`. Read it from top to bottom. Parsing, price loading, span
normalisation, the span insert, trace summaries, roll-ups, online scoring and the inbox delete are one
`tenant_session`. `AFTER_WRITE` is the list of steps that commit with the spans.

The line `AGENTOPS_TEST_PAUSE_BEFORE_COMMIT` lets a test stop the worker after everything is written and before the
commit. That is how the crash test works.

Test: `tests/test_ingest_crash.py::test_killed_ingest_worker_loses_nothing_and_duplicates_nothing`.

### 4. Idempotent writes

Open `backend/app/ingest/writer.py`: `INSERT_SPANS`, and `write_spans()`.

A span is identified by `(project, start day, trace id, span id)`. The insert is one statement that reads the
spans as JSON (`jsonb_populate_recordset`) and does `ON CONFLICT DO NOTHING ... RETURNING`. `RETURNING` gives
exactly the rows that were new, and only those feed the roll-ups and the scores. That is the whole idempotency
argument: a batch that arrives twice, or is processed twice, adds nothing the second time.

Tests: `tests/test_writer.py::test_a_batch_sent_twice_is_stored_once`,
`tests/test_ingest_worker.py::test_concurrent_workers_process_each_batch_exactly_once`.

### 5. Trace summaries in any order

Still in `writer.py`: `UPSERT_TRACES`. After the insert, every trace the new spans belong to is recomputed from all of
its spans in the database, not from the batch. Spans may arrive in any order and across several batches; the summary
is correct once they have all arrived.

Test: `tests/test_writer.py::test_trace_summary_is_correct_whatever_order_spans_arrive_in`.

### 6. Partitions, and why row-level security still holds

Open `backend/alembic/versions/0011_spans.py`: the `spans` table, `ensure_span_partition()` and
`drop_span_partition()`. Then `backend/app/ingest/partitions.py`, `ensure_partitions()`.

Spans are partitioned by day. The application role is not the owner of `spans`, so it cannot create or drop a
partition. Those two functions run with the owner's rights (`SECURITY DEFINER`), do one thing each, and revoke rights
on every new partition from `agentops_app`. So the only way to reach a day's rows is through the parent table, where
the tenant policy applies. Notice the case where a day's rows already sit in the default partition: the function
returns `false` rather than failing.

Tests: `tests/test_writer.py::test_partitions_cannot_be_read_around_the_parent_table`,
`::test_a_span_dated_far_in_the_past_is_kept_in_the_default_partition`.

### 7. Roll-ups that can be added together

Open `backend/app/ingest/rollups.py`: `LATENCY_BOUNDS_MS`, `aggregate()`, `UPSERT`, `add_to_rollups()`, `percentile()`,
and `query_rollups()`. In the database, `int_array_add` and `sum_int_array` (migration `0012`).

Roll-ups are updated incrementally from the new spans, in the ingest transaction. Latency is a histogram, so buckets
can be summed and a percentile read from the sum. The percentile is the upper bound of its slot, so it is approximate
to the width of a bucket; the docstring and the API say so. Read `percentile()` and convince yourself that the
overflow slot is reported as twice the highest bound.

Test: `tests/test_rollups.py::test_rollups_equal_an_independent_count_of_the_spans`. It compares the incremental
figures with a count computed from the spans, which is the check that catches a drift.

Reads are a second concern. The primary keys lead with `project_id`, so an organisation-wide read needs its own
index. Migration `0015` adds one, and `tests/test_rollups.py::test_an_organisation_wide_read_does_not_scan_other_organisations_rollups`
shows the problem it fixed. `query_rollups()` sums the histogram slots with plain `sum()`, not with `sum_int_array`
(see the comment on `HIST_SUM`); comparing the two is a good question to ask of the code.

### 8. Secrets and cost on arrival

Open `backend/app/security/redact.py`: `SENSITIVE_KEY` and `SECRET_VALUE`. Then `normalise()` in `writer.py`, and
`cost_for()` in `backend/app/ingest/pricing.py`.

Masking matches whole key segments, so `input_tokens` is kept and `api_key` is masked. This was a real bug: masking
on any substring hid token counts from the evaluation. The cost of a span is computed from the organisation's price
table. A model with no price gets `NULL`, not zero, and the reason is in the docstring of `cost_for`.

Tests: `tests/test_security_basics.py::test_redact_masks_sensitive_keys_and_secret_looking_values`,
`::test_counts_named_like_secrets_are_not_masked`, `tests/test_writer.py::test_secret_looking_values_are_masked_on_arrival`,
`::test_cost_uses_the_organisations_prices_and_is_unknown_without_one`.

### 9. Retention

Open `backend/app/ingest/retention.py`, `apply_retention()`. It drops whole day partitions that no project needs, and
trims the rest row by row. Read the `keep_from` query first: the longest retention across all projects decides what
can be dropped.

Tests: `tests/test_retention.py::test_partitions_older_than_every_projects_retention_are_dropped`,
`::test_old_data_goes_and_a_project_with_longer_retention_keeps_its_own`.

## Part B: evaluation

### 10. Deterministic evaluators

Open `backend/app/evals/evaluators.py`: `KINDS`, `validate_config()`, `evaluate()`.

Each evaluator returns a `Score`, and the distinction that matters is `invalid`. A score that is `invalid` means the
evaluator could not judge (no expected output, no latency recorded, a pattern that timed out). A failed score means
the output was judged and was wrong. Invalid scores are not counted as failures. Configuration is checked when the
evaluator is created (`create_evaluator` in `backend/app/api/datasets.py` calls `validate_config`), not when an
experiment runs.

Regular expressions use the `regex` library with a time limit, so a catastrophic pattern cannot hang a worker.

Tests: `tests/test_evaluators.py::test_what_cannot_be_judged_is_marked_invalid_not_failed_quietly`,
`::test_a_catastrophic_pattern_cannot_hang_a_worker`.

### 11. The judge

Open `backend/app/evals/judge.py`: `GRADE`, `SYSTEM`, `build_messages()`, `judge()`.

Three things limit a model used as a grader. The item is placed inside tags and the instructions say it is data.
The grade must come back as a call to one tool with a fixed shape. Anything else, after two attempts, is an invalid
score, never a pass. Read `judge()` and find the branch that returns the invalid score.

Tests: `tests/test_judge.py::test_anything_but_a_valid_grade_is_no_grade_never_a_pass`,
`::test_the_item_is_sent_as_data_and_the_judge_is_told_so`, `::test_one_bad_reply_is_retried`.

### 12. Calibration

Open `backend/app/evals/calibration.py`: `cohen_kappa()`, the `PAIRS` query, and `calibration()`.

Engineers label outputs themselves. The query joins a label to the experiment result with the same item and output
hash, so a judge is compared on the same output a person graded. Agreement is corrected for chance with Cohen's kappa.
A judge is `calibrated` only with at least `JUDGE_MIN_LABELS` (20) labels and kappa of at least `JUDGE_MIN_KAPPA`
(0.6). Both are read from the environment (`backend/app/config.py`); no endpoint sets them, so changing them means
restarting with a new value.

Tests: `tests/test_judge.py::test_cohen_kappa_matches_a_worked_example`,
`tests/test_experiments.py::test_human_labels_calibrate_a_judge`.

### 13. Experiments

Open `backend/app/evals/runner.py`: `CLAIM`, `advance_experiment()`, `run_pending()`, `loop()`. Then
`backend/app/evals/targets.py`: `validate_target_url()` and `call_http()`.

An experiment is a claim under a lease, like a run. Each item's result is committed on its own; an item that already
has a result is skipped. That is why an interrupted experiment resumes without scoring anything twice. Notice where
the cost limit is checked, and that judge cost counts toward it.

`validate_target_url()` resolves the address once. It always refuses link-local, multicast, unspecified and reserved
addresses (the cloud metadata address is link-local), and refuses any address that is not public when
`ALLOW_PRIVATE_TARGETS=false`. `call_http()` connects to the address it checked, sends the original
host name in the `Host` header, does not follow redirects, and caps the response size. This closes the gap where a
second DNS answer could point the connection somewhere else (DNS rebinding).

Tests: `tests/test_experiments.py::test_an_interrupted_experiment_resumes_without_scoring_any_item_twice`,
`tests/test_targets.py::test_addresses_that_are_never_allowed`,
`::test_private_addresses_depend_on_the_setting`.

### 14. The release gate

Open `backend/app/prompts/gate.py`: `check_gate()`. Then `request_release()` and `decide()` in
`backend/app/api/prompts.py`.

A prompt version becomes live only through a release request. The gate is checked twice: when the request is made,
and again when an administrator approves, because things may have changed. A request that fails is stored as
`blocked` and cannot be approved. The gate's checks are recorded with the request, so the reason is kept.

Notice the rollback path in `check_gate()`: returning to a version that was live before needs no new experiment.
Notice too that the requester cannot approve their own request unless they are the only administrator (`decide()`).

Tests: `tests/test_prompts.py::test_each_gate_check_can_block_a_release`,
`::test_an_uncalibrated_judge_cannot_carry_a_release`,
`::test_two_people_for_a_release_unless_there_is_only_one_administrator`.

### 15. A regression gate for the Investigator

Open `backend/eval/baseline.json` and `backend/tests/test_eval_gate.py`. The recorded held-out results are in
`backend/eval/results/`, and `EVALUATION.md` explains how they were produced.

The gate fails when a stored held-out result is more than 0.05 below its baseline, or when the question sets overlap.
It reads recorded files and calls no model, so it runs in every test pass.

Test: `tests/test_eval_gate.py::test_recorded_result_is_within_tolerance_of_its_baseline`.

## Part C: monitors and online scoring

### 16. Monitor windows

Open `backend/app/monitors/rules.py`: `last_complete_window()`, `measure()`, `evaluate_rules()`, `evaluate_all()`.

A rule is evaluated over the most recent complete window of roll-ups, never over the current, unfinished one. Windows
are fixed intervals of the rule's length (a 15-minute window ends at :00, :15, :30 and :45), and the job runs on the
five-minute grid (`@scheduler.every(300, on_boundary=True)`), so no window is skipped and a long window that is checked
several times still raises one alert: the insert is `ON CONFLICT DO NOTHING` on `(rule_id, window_start)`. A window
with fewer than `min_calls` calls raises nothing: an error rate of 100% over one call is noise.

Test: `tests/test_monitors.py::test_the_window_ends_at_the_last_finished_bucket`.

### 17. Online scoring

Open `backend/app/monitors/online.py`: `sampled()` and `score_online()`.

Sampling hashes the trace id, so the same traces are chosen however many times a batch is processed, and a trace is
scored completely or not at all. Only deterministic evaluators are allowed; `score_online` runs inside the ingest
transaction, and a judge there would put a model call inside ingestion. Scores are stored per span and evaluator with
a unique key, so a re-ingested batch adds no score twice.

Tests: `tests/test_online_scoring.py::test_live_answers_are_scored_once_and_feed_a_score_monitor`,
`::test_a_judge_cannot_score_live_traffic`.

## Part D: the Investigator

The agent is the part a learner most often needs to see whole. Its loop is the same design as the one in FinOps
Agent, which is why this part is short on new ideas and long on how each one is enforced.

### 18. The loop

Open `backend/app/agent/runtime.py`: `advance()` and `next_action()`.

```
load the run and its steps from the database
decide the next action from the steps alone:
    a tool call is unfinished (waiting, or left by a crash)  -> resolve it
    the last model message asked for a tool that has not been started -> start it
    otherwise -> ask the model
do exactly one action, commit it, publish an event, repeat
```

Notice what is not there: no variable that holds the conversation, and no in-memory list of pending calls.
`next_action()` takes the stored steps and nothing else. That is what makes a crash at any line recoverable: calling
`advance()` again continues correctly.

Test: `tests/test_runtime.py::test_happy_path_runs_tools_in_order_and_completes`.

### 19. The conversation is rebuilt, not remembered

Open `backend/app/agent/context.py`: `build_messages()` and `render_tool_output()`.

Each model call's messages are derived from the steps table. A tool result over 4,000 characters is not replayed; the
model sees a summary and a handle it can pass to other tools. The system prompt is the same for every run and every
organisation (`backend/app/investigator/prompts.py`), so a provider can cache it. What varies per run (the
organisation's name, its projects, the time) goes into the first user message, from `context()` in
`backend/app/investigator/agent.py`.

Test: `tests/test_runtime.py::test_conversation_is_rebuilt_identically_from_the_database`.

### 20. Tools, and who may use them

Open `backend/app/agent/tools.py`: `ToolRegistry.tool()`, `get()`, `specs()`.

A tool is an async function with a Pydantic argument model. The registry produces the JSON schema the model sees from
that same model, so the description and the validation cannot drift apart. Each tool declares its `level`
(`read`, `write` or `needs_approval`), the `roles` that may use it, its `timeout_s` and whether it is `idempotent`.
Notice `specs(role)`: a tool the role may not use is not described to the model at all.

What happens when the model gets a call wrong is in `_validate()` in `runtime.py`. An unknown tool or bad arguments
become an error message for the model, so it can correct itself. Two such errors in a row fail the run.

Tests: `tests/test_runtime.py::test_unknown_tool_and_bad_arguments_are_told_to_the_model`,
`::test_a_tool_the_role_may_not_use_does_not_exist_for_it`.

### 21. The Investigator's tools: typed, bounded, scoped

Open `backend/app/investigator/tools.py`. Read `Window`, `_check_range()` (a 31-day limit), `_result()` (a 20-row
preview and a handle to all rows), and `query_metrics`.

There is no free-form SQL tool. Each tool is a fixed, organisation-scoped read. The questions the Investigator can
ask are the questions these tools can answer, so the attack surface of a model writing SQL does not exist.

Test: `tests/test_investigator_tools.py::test_compare_windows_shows_the_bad_release`,
`::test_bad_arguments_are_explained_to_the_model`.

### 22. Text from applications is data

In `backend/app/investigator/tools.py`, `search_traces`, `get_trace` and `group_errors` pass `untrusted=True` to
`_result()`. Their output is returned under `untrusted_data`, because span names, error messages and attributes were
written by the applications being observed. The system prompt (`prompts.py`, "Security") says so, but nothing relies
on the model obeying it: a planted instruction cannot add a tool, change a role, or approve an action.

Test: `tests/test_investigator_tools.py::test_text_from_applications_is_delivered_as_untrusted_data`. The attack suite
(`backend/eval/attacks.py`, see `SECURITY.md`) tries the same thing against the whole agent.

### 23. Durability: intent before effect

Open `backend/app/agent/runtime.py`: `_start_tool()`, `_execute()`, `_record_outcome()` and `_resolve()`.

A tool call is recorded in two writes. The intent is a step in state `started`, committed before the tool runs. The
outcome is a second write. If a worker dies between them, `_resolve()` finds a `started` step with no outcome. A read
tool runs again. A tool that is not idempotent is not repeated; the model is told the action was interrupted and may
or may not have taken effect.

A tool's database effects are a `commit` hook (`ToolResult.commit`). `_record_outcome()` applies it in the same
transaction that marks the step done, so the effect and its record cannot disagree.

Tests: `tests/test_worker_crash.py::test_killed_worker_is_replaced_and_the_run_completes` (it kills a real worker
process in the middle of a tool call), `tests/test_runtime.py::test_approval_parks_the_run_without_executing`.

### 24. The queue is a table

Open `backend/app/worker/run.py`: `CLAIM`, `claim_next()`, `heartbeat()`, `release()`, `run_one()`, `serve()`.

A run is claimed by writing a worker id and a lease expiry onto its row, with `FOR UPDATE SKIP LOCKED`. A
transaction-level advisory lock makes the per-organisation concurrency limit exact when several workers claim at
once. The worker renews its lease every 10 seconds (`keep_lease` in `run_one`). If the renewal finds the lease is no
longer its own, it stops without recording anything. A run that keeps killing its workers is failed as abandoned
after `RUN_MAX_ATTEMPTS` pickups. A deploy (SIGTERM) hands unfinished runs back at once.

Tests: `tests/test_worker_claim.py::test_expired_lease_is_reclaimed_and_live_lease_is_not`,
`::test_organisation_concurrency_limit_holds_under_concurrent_claims`,
`::test_run_that_keeps_killing_workers_is_abandoned`.

### 25. Human approval

Open `backend/app/agent/runtime.py`, the `needs_approval` branch of `_start_tool()`. Then `decide()` in
`backend/app/api/approvals.py`. Then `propose_rollback` in `backend/app/investigator/tools.py`.

The step is stored as `awaiting_approval`, an approval row is created, and the run is parked. No worker holds it.
The decision is enforced on the server: the tool is not executed until the row says `approved`. `decide()` requeues
the run, and an approved action runs exactly once.

`propose_rollback` is the only action the Investigator can take. It moves a prompt back to a version that was live
before, and it runs only after approval. Read its `commit` hook: it writes an approved release request naming the
administrator who approved, and sets the live version. It does not go through the gate again, because a rollback
needs no experiment. Note that `decide()` in `approvals.py` does not apply the two-person rule that prompt releases
have, so an administrator who started the investigation can approve its rollback. That is a gap to think about.

Tests: `tests/test_investigator_tools.py::test_rollback_needs_approval_and_only_goes_to_a_version_that_was_live`,
`tests/test_approvals_api.py::test_approval_runs_the_action_once_and_is_audited`.

### 26. Budgets

Open `backend/app/agent/budgets.py`: `check_budget()`.

Before every model call the runtime checks the run's steps, tokens, working time and cost, and the organisation's and
the installation's spend for the day. Exceeding any of them ends the run as `budget_exceeded` with the evidence so far.
Nothing in a prompt can be relied on to stop a model; this can.

Tests: `tests/test_budgets.py::test_step_limit`, `::test_organisation_daily_limit_applies_across_runs`.

### 27. Calculations, not arithmetic

Open `backend/app/investigator/compute.py`: `run_compute()`, the `OPS` table at the end, and `total()`.

The model names an operation and the columns of stored results. It does not add up numbers itself, because a sum it
writes in prose would be in no evidence, and the report check would catch it. `total` sums named columns over one or
more results. It was added after a held-out evaluation showed that the Investigator could not add two projects'
costs. Read `OPS` to see the other six operations (`growth`, `share`, `margin`, `variance_decomposition`,
`zscore_anomalies`, `seasonal_forecast`).

Tests: `tests/test_compute.py::test_total_adds_one_column_across_several_results`,
`::test_total_rejects_a_column_one_result_lacks`.

### 28. Grounding: checking the report

Open `backend/app/investigator/grounding.py`, the module docstring and `check_text()`. Then `finish()` in
`backend/app/investigator/common.py`.

When the model submits its report, every number in the summary and the cited findings is looked up in the stored
evidence, with tolerance for rounding. A sentence with a number that has no source is refused once, with the list of
offending sentences, so the model can repair it. If it submits again unfixed, the sentences are removed and shown to
the reader as unsupported.

The module's docstring names the limits: the check proves a number exists in the evidence, not that the sentence
describes it correctly, and small common values can match by coincidence.

There is no unit test of `check_text()` in this repository. The nearest checks are end to end:
`tests/test_investigator_tools.py::test_the_demonstration_script_runs_end_to_end` asserts that the report has no
unsupported figures, and `tests/test_investigator_live.py` makes the same assertion against a real model. Writing the
unit test is the first exercise below.

### 29. Live updates

Open `backend/app/agent/events.py` (`publish()`, which appends to a Redis stream after each committed step) and
`backend/app/api/events.py` (`run_events()`).

The stream is read by polling every 0.4 seconds, not by a blocking read, so thousands of open pages share a small
connection pool. Each event carries its Redis id, which the browser sends back as `Last-Event-ID` on reconnect. Redis is
never the source of truth: if it were emptied, no run would be lost, and the page would reload its state from the
database.

Test: `tests/test_investigations_api.py::test_event_stream_replays_in_order_and_resumes_after_last_event_id`.

### 30. Providers and retry

Open `backend/app/llm/base.py`: `call_with_retry()`. Then `backend/app/llm/openai_compat.py`: `merge_stream()`, and
`backend/app/llm/factory.py`: `get_client()`.

The runtime knows only the shared types. `call_with_retry()` holds the policy for every provider: retry the
retryable errors with back-off and jitter, honour `Retry-After`, never retry a bad request or a wrong key, try the
fallback model once, and cap concurrency per process. `merge_stream()` exists because some gateways stream even when
asked not to.

Tests: `tests/test_llm_retry.py::test_fallback_model_is_used_after_the_primary_is_exhausted`,
`tests/test_llm_adapters.py::test_plain_and_streamed_responses_give_the_same_result`.

### 31. Tenancy behind a connection pooler

Open `backend/app/db.py` (`tenant_session()`, `system_session()`) and `backend/app/rls.py` (`enable_rls()`).

The organisation is set with `set_config(..., true)` at the start of each transaction, so it is scoped to that
transaction. Behind PgBouncer in transaction mode, a connection is shared between organisations, so nothing set at
session level could be trusted. The policy is the only filter; application code does not add `WHERE org_id = ...`.
`system_session()` opens only the six tables listed in `DATABASE.md`.

Tests: `tests/test_tenant_isolation.py::test_org_does_not_leak_between_transactions_on_a_pooled_connection`,
`::test_rows_of_one_org_are_invisible_to_another`, `::test_no_org_set_means_no_rows`.

## What is copied from FinOps, and what is new

| Copied from FinOps Agent | New in AgentOps |
|---|---|
| The agent loop and its durability (`app/agent/`, `app/worker/run.py`) | Ingestion: the inbox, the idempotent writer, the ingest worker (`app/ingest/`, `app/worker/ingest.py`) |
| Row-level security and tenant sessions (`app/rls.py`, `app/db.py`) | Partitioned spans, the partition functions and the roll-up histograms |
| LLM adapters, retry and fallback (`app/llm/`) | Deterministic evaluators, the judge, calibration, experiments and the SSRF-checked HTTP target (`app/evals/`) |
| Accounts, sessions, CSRF, rate limits, encryption, redaction (`app/security/`) | The prompt registry, the release gate and two-person approval (`app/prompts/`, `app/api/prompts.py`) |
| The Investigator's compute, charts, grounding, and `record_finding`, `ask_user`, `finish` (`app/investigator/common.py`, `compute.py`, `grounding.py`, `charts.py`) | Monitors, online scoring and the 5-minute roll-up alerts (`app/monitors/`) |
| The run API and live events (`app/api/investigations.py`, `events.py`) | The typed Investigator tools over roll-ups, traces and releases (`app/investigator/tools.py`) and `propose_rollback` |
| The approval flow for tool actions (`app/api/approvals.py`) | The applications' client and live prompt read (`client/agentops_client.py`, `/v1/prompts/{name}`) |

The build ledger (`.superpowers/sdd/2026-11-14-agentops/progress.md`) records each ruling that departs from the plan.
Read its `Ruling:` lines alongside the code when you want to know why something is the way it is.

## Exercises

1. **Write the missing grounding test.** `check_text()` in `backend/app/investigator/grounding.py` has no unit test.
   Write one first, in the style of `tests/test_investigator_tools.py`: a summary with one figure that appears in the
   evidence and one that does not. Assert that the second is listed as unsupported and is removed from the cleaned
   text. Then find the case the docstring warns about, a small value that matches by coincidence, and decide whether
   it should be a failing test.
2. **Add an evaluator kind.** Add `starts_with` to `KINDS` in `backend/app/evals/evaluators.py`, with its `validate_config`
   branch and its `evaluate` branch. Write its tests first in `tests/test_evaluators.py`. Check that `create_evaluator`
   in `backend/app/api/datasets.py` refuses a bad configuration.
3. **Kill an ingest worker in the middle of a batch.** Read `tests/test_ingest_crash.py`, then run the same scenario by
   hand: start the stack's `ingest` worker with `AGENTOPS_TEST_PAUSE_BEFORE_COMMIT`, kill it while it pauses, start
   another, and count the spans and the inbox rows. Explain why the counts are right.
4. **Add a compute operation.** Follow `total()` in `backend/app/investigator/compute.py`: its argument check, its
   result table, its entry in `OPS`, and the schema the model sees in `common.py`. Write its test in
   `tests/test_compute.py` first.
