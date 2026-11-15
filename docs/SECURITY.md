# Security

What is defended, how, and what is not. Controls are listed with the test that exercises them.

## What is different about an observability platform

AgentOps reads text that it did not write and cannot vet. Any application holding an ingestion key can put
any string into a span's name, error message, attributes or content, and the Investigator reads those strings
while it works. So the telemetry itself is the main injection channel, and an ingestion key is enough to use
it. The attack suite plants instructions in error messages and in span attributes for exactly this reason
(`EVALUATION.md`).

## Position on prompt injection

Prompt injection is treated as unsolved. The instructions tell the model that anything under
`untrusted_data` is what the applications wrote and not an instruction, and in the attack suite Claude
Sonnet 5.5 spotted the planted text, named it as injected and ignored it. Nothing depends on that. The design
question is: *if the model were fully controlled by an attacker, what could it do?*

| It could | It could not |
|---|---|
| Read metrics, traces and error groups of any project in the organisation of the person who asked | Read another organisation's data, user accounts, sessions, ingestion keys, provider keys or prompt texts |
| Spend that run's budget | Exceed the per-run, per-organisation or global daily limits |
| Write a misleading report | Put a figure in the report that is not in the stored evidence |
| Propose a prompt rollback | Carry one out: an administrator must approve it, and only to a version that was live before |
| | Run a query of its own (there is no query tool), change data, call a tool its user's role does not have, or reach the network |

The worst outcome of a successful injection is a wrong or misleading report about telemetry the user was
already allowed to see, a wasted budget, and a rollback proposal that an administrator then has to reject.

## Controls, mapped to the OWASP Top 10 for LLM Applications (2025)

| Risk | Control | Proven by |
|---|---|---|
| **LLM01 Prompt injection** | Privilege separation as above. Text from applications is returned under `untrusted_data`. The `content` column (prompts and completions) is never given to the model by any tool; attributes are cut to 12 keys of 200 characters. | `eval/attacks.py` (injection_error, injection_content); `tests/test_investigator_tools.py::test_text_from_applications_is_delivered_as_untrusted_data` |
| **LLM02 Sensitive information disclosure** | Ingested spans are masked on the way in (`security/redact.py` on status messages, attributes and content) before they are stored. Ingestion keys are stored as SHA-256 and shown once. No tool reads keys, users or settings. | `tests/test_security_basics.py`; `tests/test_writer.py::test_secret_looking_values_are_masked_on_arrival`; `eval/attacks.py` (ingest_keys, cross_org) |
| **LLM03 Supply chain** | Dependencies pinned by lock files (`uv.lock`, `package-lock.json`). Containers run as a non-root user. | not tested |
| **LLM04 Data and model poisoning** | No training or fine-tuning. Telemetry can be poisoned by any key holder; that is LLM01 above, and roll-ups are recomputed from stored spans, never from model output. | not applicable |
| **LLM05 Improper output handling** | Tool arguments are validated by schema; time windows over 31 days are refused. Model text is rendered by React as text, never as HTML. | `tests/test_investigator_tools.py::test_bad_arguments_are_explained_to_the_model`; `eval/attacks.py` (unbounded) |
| **LLM06 Excessive agency** | Read-only tools, one action tool (`propose_rollback`, level `needs_approval`). The server checks the approval row. A prompt release by a person is always decided by an administrator; a release that fails its gate is stored as blocked and cannot be approved, and when the organisation has two administrators the requester cannot approve their own. | `tests/test_investigator_tools.py::test_rollback_needs_approval_and_only_goes_to_a_version_that_was_live`; `tests/test_approvals_api.py`; `tests/test_prompts.py::test_two_people_for_a_release_unless_there_is_only_one_administrator`; `eval/attacks.py` (release) |
| **LLM07 System prompt leakage** | The system prompt holds no secret or tenant data; the organisation and project names are added per run. The model is asked to decline. | `eval/attacks.py` (prompt_leak) |
| **LLM08 Vector and embedding weaknesses** | No vector store is used. | not applicable |
| **LLM09 Misinformation** | The model does no arithmetic (`compute` does, in Decimal). Every figure in a report is matched against stored evidence; unsupported ones are removed and shown as removed. An LLM judge that returns anything but a valid grade never counts as a pass, and an uncalibrated judge cannot carry a release. | `tests/test_compute.py`; `tests/test_judge.py`; `tests/test_prompts.py::test_an_uncalibrated_judge_cannot_carry_a_release` |
| **LLM10 Unbounded consumption** | Per-run limits on steps, tokens, time and cost; per-organisation and global daily cost limits; 31-day window cap; per-key ingestion rate limit (600 requests a minute); 4 MiB body cap applied after decompression; at most 10,000 spans per batch. | `tests/test_budgets.py`; `tests/test_ingest_endpoint.py::test_oversized_and_decompression_bomb_bodies_are_refused`; `tests/test_ingest_endpoint.py::test_requests_over_the_per_key_limit_get_429`; `eval/attacks.py` (budget, unbounded) |

## Ingestion

| Concern | Decision |
|---|---|
| Keys | `ak_<prefix>_<secret>`; only the SHA-256 is stored; scopes `ingest` and `prompts:read`; a key writes only to its own project; lookups are cached in Redis for a short time; revoking a key clears its cache entry, and a request that was reading the key while the revoke committed is refused rather than cached |
| Body | protobuf or JSON, optionally gzip; refused if the declared or actual size, or the size after decompression, is over 4 MiB, so a small compressed body cannot expand without bound |
| Durability | a batch is committed to the inbox table before `200` is sent; if it cannot be stored the sender gets `503` and retries, as the OTLP specification says |
| Poison batches | a batch that fails to parse is retried under a lease, then parked as `dead` for an administrator to replay or discard; it never blocks other batches |

`tests/test_ingest_endpoint.py`, `tests/test_ingest_worker.py`, `tests/test_ingest_crash.py`.

## Experiments that call your application

An experiment can call an HTTP endpoint you give it. The address is resolved once, checked (link-local
addresses, including the common 169.254.169.254 metadata address, always refused; private networks refused when
`ALLOW_PRIVATE_TARGETS=false`), and the connection is then made to that checked address, so a second DNS answer
cannot redirect it (DNS rebinding). Responses are size-capped. `tests/test_targets.py`.

Some clouds put their metadata service on other addresses (for example `fd00:ec2::254` or `100.100.100.200`);
with the default `ALLOW_PRIVATE_TARGETS=true` those are allowed, so **set it to false on any cloud host.**

JSON-schema evaluators may only refer to their own parts (`#/...`); a schema is validated with a registry that
cannot fetch anything, so an evaluator cannot be used to make the workers request a URL
(`tests/test_evaluators.py::test_a_stored_schema_with_a_remote_reference_is_never_fetched`).

## Tenancy

Row-level security is enabled and forced on every table that holds an organisation's data, including the
day partitions of `spans`; the organisation is set per transaction with `SET LOCAL` semantics, which is safe
behind PgBouncer in transaction mode. Workers use a system session, which opens only the six tables that must
be read across organisations (`projects`, `ingest_keys`, `ingest_batches`, `runs` and `experiments` for
claiming work, `users` for sign-in); every other table stays closed to it. Objects of another organisation return 404.
`tests/test_tenant_isolation.py`, `tests/test_roles.py`, `tests/test_ingest_endpoint.py::test_a_key_writes_only_to_its_own_project`.

## Accounts and sessions

Unchanged from FinOps Agent: scrypt passwords, HttpOnly session cookie with only its hash stored, CSRF
header, sign-in rate limits, roles viewer / engineer / admin checked on every route, an append-only audit
log, security headers, and validation of the model base URL an administrator sets.

## Known limitations

- **Anyone with an ingestion key can plant text the Investigator reads.** Containment is above; a key should
  be treated as a credential that can influence reports, not only as a way to send data.
- **Masking on ingest is pattern-based.** It catches keys named like secrets and values that look like
  tokens; a secret in free text with no recognisable shape is stored as sent. Applications should not send
  prompt and completion content unless they need it (FinOps sends it only with `OTEL_CAPTURE_CONTENT=true`).
- **The default `SECRET_KEY` in `docker-compose.yml` is public.** It exists so the demo starts with one
  command. Any real deployment must set its own.
- **Demo accounts with published passwords** are created when `SEED_DEMO=true`.
- **The web port follows `WEB_BIND`** (127.0.0.1 by default). It is also published on the Docker bridge gateway
  (`WEB_INGEST_BIND`) so other projects' containers can send traces; that address is not reachable from the network,
  but any container on the default bridge can reach the sign-in page. Set both to a firewalled address for a shared host.
- **Secrets in telemetry are masked on arrival**: API keys (`sk-`, `pk-`, `rk-`), Bearer tokens, AWS access key ids, GitHub,
  Slack and Google keys, JSON Web Tokens and PEM private key blocks (`app/security/redact.py`). A secret in a shape
  not on that list is stored as sent.
- **The API port (8200) is published on 127.0.0.1 only.** The API trusts `X-Forwarded-For` because it sits
  behind nginx, so it must never be published to a network directly.
- **`/metrics` is open unless `METRICS_TOKEN` is set** (reachable from the host only, with the default ports).
- **Open sign-up shares the server's model key** within `GLOBAL_DAILY_COST_USD`; set `ALLOW_SIGNUP=false` if
  strangers can reach the installation.
- **Telemetry stays accepted while Redis is down**: key lookups fall back to the database and the per-key
  ingestion limit is not enforced until Redis returns.
- **Private addresses are allowed by default** for model base URLs and experiment targets, because a local
  gateway or a local application needs it. Set both `ALLOW_PRIVATE_*` settings to false on a shared
  installation.
- **No multi-factor authentication, single sign-on, or password reset by e-mail.**
- **Injection resistance was shown on eight cases by one model.** A second model (gpt-oss-120b) held its
  cases mostly because its runs failed early, which says the boundaries held, not that it resisted. It is
  evidence, not proof. No independent penetration test has been done.
