# AI assistance disclosure

AgentOps Control Plane was developed with AI coding assistance. This document states what was generated, what was
decided by the maintainer, and how the result was verified.

## Tools

| Tool | Use |
|---|---|
| Claude (Anthropic), via Claude Code | Code generation, refactoring, tests, evaluation and attack scripts, documentation, debugging, running the Docker stack and the evaluations |
| Playwright | Browser journeys in `frontend/e2e/`; the README screenshots, captured from the running stack by browsing only |
| Archify | Generation and validation of the architecture diagram in `docs/architecture.html`, with its PNG in `docs/architecture.png` |
| Hallmark design skill | UI design rules for the web interface |
| An LLM (optional, runtime) | The Investigator's answers and the LLM judge, when a model is configured; see [PROMPTS.md](PROMPTS.md) |

## Division of responsibility

The maintainer defined the requirements, made the architecture and scope decisions, reviewed every change and every
measured result, and approved what was merged. The assistant implemented under that direction. The design decisions and
the reasons for them are recorded in [ARCHITECTURE.md](ARCHITECTURE.md); the assistant did not choose them independently.

Design decisions recorded in the repository include:

- A Python backend in which the API, the ingest workers and the agent and experiment workers coordinate only through
  PostgreSQL, with Redis for wake-up signals and live events, and PgBouncer in front of the database.
- Row-level security as the tenancy boundary, with one database role for migrations and another for the application.
- An Investigator built from typed, read-only tools and fixed calculations, with no free-form query tool, and a report
  whose figures are checked against stored evidence.
- Evaluation on a seeded, planted-incident dataset of 40 questions, with the held-out set run once per model and the
  results reported with their failures.

## Verification

- Backend tests in `backend/tests`, run against a real PostgreSQL and Redis. Results for the run recorded with this
  repository are in [`docs/test-results`](test-results): `junit.xml` and `pytest-output.txt`. The count is
  340 tests: 340 passed, 0 failed, 0 skipped. Tests that call a
  real model are marked `live` and were not run for this record.
- Frontend browser journeys in `frontend/e2e/` (sign-in and roles, isolation between organisations, the release gate,
  monitors and traffic) are written in Playwright and run against the running stack with `npx playwright test`. They were
  not run for this record.
- The Investigator evaluation on held-out data and the attack suite, with their raw output in
  `backend/eval/results/` and the method, numbers and failures in [EVALUATION.md](EVALUATION.md).
- The regression gate `backend/tests/test_eval_gate.py` fails when a stored held-out result falls more than 0.05 below
  its baseline in `backend/eval/baseline.json`.

## Data

No AI-generated data is presented as real. The demo telemetry is produced by a deterministic generator in this
repository (`backend/seed/generate.py`), with five planted incidents described in [EVALUATION.md](EVALUATION.md). The
evaluation questions and their reference answers are computed by SQL (`backend/eval/build_questions.py`); no model
writes them.

## Runtime use of AI

With the default `LLM_PROVIDER=stub`, no model is called: investigations run a fixed script, and the report says so. When
a provider is configured, the Investigator sends its tool results and the judge sends the output it grades to that
provider. Telemetry can contain customer text, so use a local model (Ollama) or no model for sensitive data; see
[PROVIDERS.md](PROVIDERS.md). The prompts are reproduced in [PROMPTS.md](PROMPTS.md). Every figure the Investigator
reports must match a stored result, and an invalid judge grade is never a pass.

## Limitations

- The account and session code uses standard-library primitives (scrypt, SHA-256, HMAC) and the `cryptography` package
  rather than custom cryptography. It is covered by automated tests and maintainer review, not by an independent security
  audit.
- The Investigator evaluation uses 13 held-out questions and one substitute model; differences of one or two questions
  are within noise.
- The LLM judge is calibrated against a small number of human labels per evaluator.
