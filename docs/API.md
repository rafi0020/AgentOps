# API reference

The routes below are taken from the routers in `backend/app/api/` and from `/v1/traces` and `/v1/prompts/{name}`.
The interactive schema, with request and response models, is served at `/docs` on the API
(http://localhost:8200/docs when running with Docker Compose).

Role column: **Any** means any signed-in person (viewer, engineer or admin); **Engineer+** means engineer or
admin; **Admin** means admin only; **Key** means an ingestion key with the scope named.

## Conventions

**Authentication.** People sign in with `POST /api/auth/login`. The response sets the `agentops_session` cookie
(HttpOnly, SameSite=Lax, Secure when `COOKIE_SECURE=true`) and returns a `csrf_token`. Every state-changing
request (POST, PUT, PATCH, DELETE) on a cookie-authenticated route must send that value in the `X-CSRF-Token`
header; `GET /api/auth/me` returns it again.

Applications authenticate with an ingestion key: `Authorization: Bearer ak_<prefix>_<secret>`. A key has one or
both of two scopes: `ingest` (send traces to `/v1/traces`) and `prompts:read` (read live prompts from
`/v1/prompts/{name}`). A new key has `ingest` only unless its creator asks for more. A key writes only to its own
project. Keys carry no cookie and need no CSRF header.

**Errors.** Bodies are `{"detail": "..."}`. Status codes:

| Status | Meaning |
|---|---|
| 401 | Not signed in, or the ingestion key is missing, unknown or revoked |
| 403 | The role does not allow it, the CSRF token is missing or wrong, or the key lacks the scope |
| 404 | Not found. Objects of another organisation look the same as missing ones |
| 409 | Conflicting state (for example, deciding a release that is already decided) |
| 413 | Body larger than 4 MB (before or after decompression), or an upload larger than 5 MB |
| 422 | Invalid input, including a time range over 31 days |
| 429 | Rate limit or organisation run limit reached; `Retry-After` says when to try again |
| 503 | The batch could not be stored (`Retry-After: 5`), or the rate limiter's Redis is unavailable |

**Pagination.** Trace lists use keyset pagination. A response includes `next_cursor` when there is another page;
pass it back as `cursor` to continue. The cursor is `start_time|trace_id`, and pages neither overlap nor skip
when new traces arrive. Other lists use `limit` with `offset` (dataset items, experiment results), `limit` alone,
or `before` (audit entries by id, runs and approvals by timestamp).

**Time ranges.** Routes that take `from` and `to` need timezone-aware ISO timestamps, for example
`2026-11-14T09:00:00Z`. A range may be at most 31 days, so no request can scan the whole history. Trace lists
default to the last 24 hours. The Investigator's tools apply the same 31-day limit.

**Content.** Prompts and outputs can hold customer data. The trace detail returns span content only to engineers
and administrators; a viewer sees that content exists (`has_content`) but not its text.

## Auth

| Method | Path | Role | What it does |
|---|---|---|---|
| POST | `/api/auth/signup` | public, rate limited | Creates an organisation and its first administrator. Refused when `ALLOW_SIGNUP=false` |
| POST | `/api/auth/login` | public, rate limited | Signs in; sets the session cookie; returns the CSRF token |
| POST | `/api/auth/logout` | Any | Ends the session |
| GET | `/api/auth/me` | Any | The signed-in person and the CSRF token |
| POST | `/api/auth/password` | Any | Changes the caller's password |

## Users

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/users` | Admin | People of the organisation |
| POST | `/api/users` | Admin | Create a person: `email`, `name`, `role` (`viewer`, `engineer` or `admin`), `password` (201) |
| PATCH | `/api/users/{user_id}` | Admin | Change `name`, `role` or `is_active`. The last active administrator cannot be demoted or deactivated |

## Ingestion

| Method | Path | Role | What it does |
|---|---|---|---|
| POST | `/v1/traces` | Key `ingest` | OTLP/HTTP trace export, protobuf or JSON, optionally gzip. Stores the batch and returns `200`. A span that ends before it starts, has a malformed id or a timestamp outside the range of dates is dropped, the rest is stored, and the response says how many were dropped (`partialSuccess.rejectedSpans`, in the sender's encoding). A body that is not OTLP is `400` and nothing is stored |
| GET | `/api/ingest/dead` | Admin | Batches that failed five times, with their errors, and the number still pending |
| POST | `/api/ingest/dead/{batch_id}/replay` | Admin | Puts a dead batch back in the queue |
| POST | `/api/ingest/dead/{batch_id}/discard` | Admin | Deletes a dead batch (204) |

## Live prompts (for applications)

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/v1/prompts/{name}` | Key `prompts:read` | The live version of a prompt in the key's project: `name`, `version`, `text`, `variables`, `model_settings`. Sends an `ETag`; `If-None-Match` returns `304` |

## Projects and prices

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/projects` | Any | List projects |
| POST | `/api/projects` | Admin | Create a project (`name`, `retention_days` 1 to 3650, default 30) |
| PATCH | `/api/projects/{project_id}` | Admin | Rename or change retention |
| GET | `/api/projects/{project_id}/keys` | Admin | List ingestion keys (prefix, name, scopes; never the secret) |
| POST | `/api/projects/{project_id}/keys` | Admin | Create a key with `name` and `scopes`; the full key is returned once |
| DELETE | `/api/projects/{project_id}/keys/{key_id}` | Admin | Revoke a key (204); takes effect on the next request |
| GET | `/api/prices` | Any | Model prices of the organisation, used to estimate span costs |
| PUT | `/api/prices` | Admin | Set the price of one model (USD per million input and output tokens) |

## Traces and sessions

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/traces` | Any | Trace list for a project, newest first. Filters: `status`, `model`, `tool`, `service_version`, `session`, `min_duration_ms`, `q` (root name), `from`, `to`, `cursor`, `limit` (1 to 100) |
| GET | `/api/traces/{project_id}/{trace_id}` | Any | One trace: summary and spans in order, with depth, timing, errors, attributes and (for engineers and admins) content. At most 2,000 spans |
| GET | `/api/sessions` | Any | Sessions of a project in a range: traces, cost, errors, tokens |
| GET | `/api/sessions/{project_id}/{session_id}` | Any | The traces of one session (up to 500) |

## Metrics and usage

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/metrics/overview` | Any | Totals for a range, the same for the range before it, model calls alone, and totals by project. Optional `project` |
| GET | `/api/metrics/series` | Any | Figures over time, grouped by any of `model`, `operation`, `tool`, `prompt_name`, `prompt_version`, `service_version`. `step` 5m, 1h or 1d. Filters `model`, `operation`, `tool` |
| GET | `/api/usage` | Engineer+ | Model calls, tokens and estimated cost of the Investigator's runs, by person and in total: administrators see the organisation, engineers see their own |

Figures come from the 5-minute roll-ups. Latency percentiles are read from a histogram, so they are the upper
bound of a bucket and approximate to its width. Responses are cached for 30 seconds per organisation.

## Monitors and alerts

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/monitors/rules` | Any | Rules, optionally for one project |
| POST | `/api/monitors/rules` | Engineer+ | Create a rule: `metric` (`error_rate`, `p95_latency_ms`, `cost_per_hour`, `score_avg`), `comparison` (`<` or `>`), `threshold`, `window_minutes` (5, 15, 30, 60, 180, 360 or 1440), `min_calls`, `filter`; `score_avg` needs an `evaluator_id` |
| PATCH | `/api/monitors/rules/{rule_id}?status=` | Engineer+ | Set `active` or `paused` |
| GET | `/api/monitors/alerts` | Any | Alerts, latest window first; `open_only`, `limit` |
| POST | `/api/monitors/alerts/{alert_id}/ack` | Engineer+ | Acknowledge an alert |
| POST | `/api/monitors/alerts/{alert_id}/investigate` | Engineer+ | Start an investigation on the alert (202). Asking again returns the one already started |
| POST | `/api/monitors/evaluate` | Engineer+ | Check every active rule now (6 requests a minute) |
| GET | `/api/monitors/online` | Any | Online evaluators of a project |
| PUT | `/api/monitors/online` | Engineer+ | Attach a deterministic evaluator to a project with a `sample_rate` between 0 and 1 |

## Datasets, evaluators and labels

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/datasets` | Any | Datasets of a project |
| POST | `/api/datasets` | Engineer+ | Create a dataset (`project_id`, `name`, `description`) |
| DELETE | `/api/datasets/{dataset_id}` | Engineer+ | Delete a dataset and its items (204) |
| GET | `/api/datasets/{dataset_id}/items` | Any | Items in order (`limit` up to 500, `offset`) |
| POST | `/api/datasets/{dataset_id}/items` | Engineer+ | Add items: each has `input`, optional `expected`, optional `metadata` |
| POST | `/api/datasets/{dataset_id}/items/upload` | Engineer+ | Add items from a CSV (an `input` column, optional `expected`; other columns become metadata) or a JSON list. At most 5 MB |
| POST | `/api/datasets/{dataset_id}/items/from-traces` | Engineer+ | Add items made from up to 200 traces |
| GET | `/api/evaluators` | Any | Evaluators of a project |
| POST | `/api/evaluators` | Engineer+ | Create an evaluator: `kind` is `exact`, `contains`, `regex`, `numeric`, `json_schema`, `latency_under`, `cost_under` or `judge`; `config` is checked when it is created |
| GET | `/api/evaluators/{evaluator_id}/calibration` | Any | The judge's agreement with human labels: count, exact agreement, Cohen's kappa, status and the disagreements |
| POST | `/api/labels` | Engineer+ | Record a human grade (1 to 5) for one experiment result |

## Experiments

| Method | Path | Role | What it does |
|---|---|---|---|
| POST | `/api/experiments` | Engineer+ | Queue an experiment (202): a dataset, a target and one or more evaluators. The target is `{"kind": "prompt", "prompt_id", "version"}` or `{"kind": "http", "url", "headers", "output_path"}` |
| GET | `/api/experiments` | Any | Experiments of a project (`limit` up to 200) |
| GET | `/api/experiments/compare?a=&b=` | Any | Two experiments side by side: per-evaluator figures, regressions and improvements, and per-item differences (up to 500). `409` when the experiments ran on different datasets |
| GET | `/api/experiments/{experiment_id}` | Any | One experiment: totals per evaluator and its results (`limit` up to 500, `offset`) |
| POST | `/api/experiments/{experiment_id}/cancel` | Engineer+ | Stop an experiment before its next item |

An HTTP target's address is checked before it is called and the connection is made to the checked address; see
`docs/SECURITY.md`.

## Prompts and releases

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/prompts` | Any | Prompts of a project with their live version |
| POST | `/api/prompts` | Engineer+ | Create a prompt with its first version (`name`, `text`, `model_settings`) |
| GET | `/api/prompts/id/{prompt_id}` | Any | A prompt with its gate settings, all its versions (newest first) and its last 50 release requests |
| POST | `/api/prompts/id/{prompt_id}/versions` | Engineer+ | Add a version. Versions are never edited |
| PATCH | `/api/prompts/id/{prompt_id}/gate` | Admin | Set the primary evaluator, `min_score` and `max_drop` for the release gate |
| POST | `/api/prompts/id/{prompt_id}/releases` | Engineer+ | Request that a version become live. Names the experiment that tested it. The gate runs now: a failing request is stored as `blocked`. The gate needs the primary evaluator to have graded at least `GATE_MIN_GRADED` items (default 20) |
| GET | `/api/releases?status=` | Any | Release requests; default `pending`. Also `blocked`, `approved`, `rejected`, `all` |
| POST | `/api/releases/{release_id}/decide` | Admin | Approve or reject. The gate is checked again on approval; a request that no longer passes becomes `blocked` (409). The requester cannot decide unless they are the only administrator |

## Investigations

An investigation is a run of the Investigator agent. Routes are under `/api/investigations`.

| Method | Path | Role | What it does |
|---|---|---|---|
| POST | `/api/investigations` | Engineer+ | Ask a question (202). Optional `project_id` and `trace_id` start the investigation from them |
| GET | `/api/investigations` | Engineer+ | Recent runs; `limit`, `before`, `mine` |
| GET | `/api/investigations/{run_id}` | Any | The run: status, steps (tool inputs and summaries), pending approval or question, and the report |
| GET | `/api/investigations/{run_id}/evidence/{handle}` | Any | One stored result (`ev_...`) that a report cites, with up to 500 rows |
| POST | `/api/investigations/{run_id}/cancel` | Engineer+, and the person who asked or an admin | Cancel a queued, waiting or running run |
| POST | `/api/investigations/{run_id}/answer` | Engineer+, and the person who asked or an admin | Answer the agent's clarifying question; the run goes back to the queue |
| GET | `/api/investigations/{run_id}/events` | Any | Live events as Server-Sent Events (below) |

### Live events

`GET /api/investigations/{run_id}/events` is a Server-Sent Events stream. Each event has an `id`; a reconnecting
client sends the last one back as `Last-Event-ID` and receives everything after it. Event types:

| Type | When |
|---|---|
| `run.status` | The run changed status. The stream closes after a final status (`completed`, `failed`, `cancelled`, `budget_exceeded`) |
| `step.started` | A model or tool step began |
| `step.finished` | A step was committed, with its summary and handle |
| `approval.requested` | A tool needs an administrator's approval |
| `question.asked` | The agent asked the person a question |
| `report.ready` | The report was stored |

The stream checks the database at least every 15 seconds, so a run that ended while nobody was listening is
still closed.

## Approvals

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/approvals?status=` | Engineer+ | Approval requests (default `pending`). Engineers see only the ones they asked for |
| POST | `/api/approvals/{approval_id}/decide` | Admin | Approve or reject with an optional reason. The run is requeued; an approved action runs once |

## Settings

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/settings/llm` | Admin | The model settings in effect for the organisation. The key is never returned, only whether one is set |
| PUT | `/api/settings/llm` | Admin | Save the model settings: `provider`, `model`, `base_url`, `api_key` (omit to keep it, empty to clear), `fallback_model`, prices. The base URL is checked |
| POST | `/api/settings/llm/test` | Admin | Send one tiny request with the saved settings and report whether it worked (6 a minute) |

## Audit

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/audit` | Admin | Audit entries, newest first; `limit`, `before` (an id), `action` (exact or prefix such as `release.`) |

The audit log is append-only for the application role.

## Evaluation of the Investigator

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/eval/results` | Any | Recorded evaluation and attack-suite results from `backend/eval/results/`. Runs in which the provider never answered are listed apart |

## Health and metrics

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/healthz` | public | The process is up |
| GET | `/readyz` | public | Database and Redis answer; `503` with the failing parts otherwise |
| GET | `/metrics` | public, or `Authorization: Bearer <METRICS_TOKEN>` when that is set | Prometheus metrics. Not part of the API schema |

## Sending traces

### With the Python client

`backend/client/agentops_client.py` is one file with no dependency on the rest of the repository. Copy it into the
application.

```python
from agentops_client import AgentOps

ops = AgentOps("http://localhost:3200", "ak_...")        # a key with the ingest scope
tracer = ops.init_tracing("checkout-assistant", "3.2.0")  # OpenTelemetry tracer exporting to AgentOps

with tracer.start_as_current_span("chat model-fast") as span:
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", "model-fast")
    span.set_attribute("gen_ai.usage.input_tokens", 812)
    span.set_attribute("gen_ai.usage.output_tokens", 96)
```

To read a live prompt, create a second key with the `prompts:read` scope and call `ops.get_prompt("order-lookup")`.
Its `stamp(span)` method records which prompt version produced a call (`agentops.prompt.name` and
`agentops.prompt.version`). Roll-ups and the Investigator's `query_metrics` group by those values, so a bad
release can be found by version.

### With a standard OpenTelemetry exporter

Any OpenTelemetry SDK that exports OTLP over HTTP works. Set its environment as follows (the header value is
URL-encoded, as the OpenTelemetry specification defines):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3200      # the SDK adds /v1/traces
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20ak_..."
export OTEL_EXPORTER_OTLP_COMPRESSION=gzip                    # optional; gzip is accepted
export OTEL_SERVICE_NAME=checkout-assistant
export OTEL_RESOURCE_ATTRIBUTES="service.version=3.2.0"
```

The API accepts protobuf and JSON bodies. Span attributes are read with the OpenTelemetry GenAI names
(`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.tool.name`, `gen_ai.provider.name`), with the older names (`gen_ai.system`,
`gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens`) also understood. `session.id` (on the span or the
resource) groups traces into sessions. Content is taken from `gen_ai.input.messages` and `gen_ai.output.messages`
(and the older `gen_ai.prompt` and `gen_ai.completion`), only when the application sends it.
