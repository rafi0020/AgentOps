# Model providers

AgentOps uses a language model in three places, and each can be configured separately:

| Use | Where it is set | Model | Notes |
|---|---|---|---|
| The Investigator | Environment or Administration → Model | The organisation's model | Runs the agent loop; cost counts against run and daily budgets |
| LLM judge (evaluators of type `judge`) | Same as the Investigator | The same model, through the same client | No separate judge model exists; see below |
| Experiment targets of type `prompt` | Same as the Investigator | The same model, with the prompt version's settings | An HTTP target calls your own endpoint instead |

## How the model is chosen

Two places configure a provider. The environment is the default for every organisation; the Administration screen
overrides it per organisation.

- **Environment** (`.env` in the `agentops/` folder, read by Docker Compose): `LLM_PROVIDER`, `LLM_MODEL`,
  `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_FALLBACK_MODEL`, `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK`,
  `ALLOW_PRIVATE_LLM_URLS`, `LLM_MAX_CONCURRENCY` (16 model calls in flight per process), `LLM_MAX_OUTPUT_TOKENS`
  (4096).
- **Administration → Model** (`PUT /api/settings/llm`, administrators only): provider, model, base URL, fallback
  model, prices and the API key. The key is encrypted with a key derived from `SECRET_KEY`, is never shown again, and
  `GET /api/settings/llm` says only whether one is set.

When an organisation has saved settings, they replace the environment for that organisation as a whole: the
provider, model, base URL, key and prices all come from the saved row, and the environment values are ignored.
If nothing is saved, the environment applies. A saved key is therefore not overridden by changing `.env`.

"Test the saved settings" in the Model screen calls `POST /api/settings/llm/test`. It sends one request, `Reply with
the single word: ok`, with a 16-token limit, and reports whether it worked, the model's reply and the latency. It is
limited to six requests a minute.

`LLM_PROVIDER` must be one of `openai_compat`, `anthropic`, `ollama` or `stub`. The service refuses to start with any
other value (`backend/app/config.py`, `known_provider`), so a typo cannot silently run the scripted stub. A model
configured under Administration → Model that cannot be used is reported by `/api/settings/llm/test`.

## Providers

The runtime knows only `Message`, `ToolSpec`, `ToolCall` and `LLMResponse` (`backend/app/llm/types.py`). Each
adapter translates to and from its provider's wire format.

| `LLM_PROVIDER` | Adapter | Endpoint | Key | Notes |
|---|---|---|---|---|
| `openai_compat` | `app/llm/openai_compat.py` | `{base}/chat/completions`. `LLM_BASE_URL` is required | Sent as a Bearer token when set | Any server that speaks the OpenAI chat API with tools. Always sends `"stream": false`; if a gateway streams anyway, `merge_stream` folds the chunks into the same shape |
| `anthropic` | `app/llm/anthropic.py` | `https://api.anthropic.com` unless `LLM_BASE_URL` is set | `x-api-key` header | Marks the system prompt and the last tool definition with `cache_control`, so the stable prefix is cached |
| `ollama` | `app/llm/openai_compat.py` (same class) | `http://localhost:11434/v1` unless `LLM_BASE_URL` is set | Optional | Ollama's OpenAI-compatible endpoint |
| `stub` | `app/llm/stub.py` | none | none | Scripted responder (`STUB_RESPONDER`, default `app.investigator.stub_agent:demo_responder`). The application works end to end without any key; its reports say they come from a script |

Timeouts are 10 seconds to connect, 120 seconds to read and 30 seconds to write. A connection failure is retryable,
and so is a response body that cannot be read (`test_unreadable_body_is_a_retryable_error`).

### Retry, fallback and concurrency

`call_with_retry` (`backend/app/llm/base.py`) is the policy every provider shares:

1. A timeout, or a status of 408, 409, 425, 429, 500, 502, 503, 504 or 529, is retried up to four attempts in all.
   The wait doubles from 0.5 seconds, is capped at 30 seconds, has ±20% jitter, and is replaced by the provider's
   `Retry-After` when it sends one.
2. Any other error, such as a bad request or a wrong key, is raised at once, because repeating it cannot succeed.
3. When the primary model is exhausted and `LLM_FALLBACK_MODEL` is set and different, the fallback gets one attempt.
4. If that fails too, the run ends with the provider's message.

A semaphore caps model calls in flight per process at `LLM_MAX_CONCURRENCY`, so a burst of runs produces waiting
instead of a burst of 429s.

## Prices and budgets

Two price tables exist, and they answer different questions.

| Setting | What it prices | Where it is used |
|---|---|---|
| `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK` (or the same fields in Administration → Model) | The Investigator's own model calls | Run cost, the per-run cost limit (`RUN_MAX_COST_USD`, default 1.00) and the daily limits. Cached input is charged at one tenth of the input price |
| The `prices` table (Administration → Prices; `GET` and `PUT /api/prices`) | Each span an application sent | Cost shown on traces and in charts. Computed when the span arrives and stored with it |

Both defaults are 3 and 15 USD per million tokens, which are placeholders. Set them to what your provider charges,
or the limits are enforced against the wrong figures. A span whose model has no price in `prices` has no cost
(`NULL`), not a cost of zero. Changing a price affects only spans that arrive afterwards.

Experiments have their own cost limit, `EXPERIMENT_MAX_COST_USD` (default 5.00), per experiment. Judge calls made
by an experiment are counted in that experiment's cost.

## The judge

An LLM judge has no model of its own. `app/evals/judge.py` calls the same client as the Investigator (the
organisation's model, or the environment's), with a single tool, `grade`, that must return a score from 1 to 5 and a
reason. Any other reply counts as no grade, never as a pass, after two attempts. Its output is a score and an
`invalid` flag; a judge that is not calibrated against people cannot carry a prompt release (see `docs/EVALUATION.md`
and the calibration section of `LEARNING.md`).

To use a different model for judging, you would add a setting and a second client. This is not in the code today,
and judge quality depends on the model that is configured for the whole organisation.

Judges are not used on live traffic. Online scoring accepts only deterministic evaluators, so no model call sits
inside ingestion.

## Concrete setups

Put these in `agentops/.env` for the Docker stack, or in the Administration screen. Replace every `<...>` with your
value. Keys belong only in `.env` or the screen, never in the repository.

From inside a container, `localhost` is the container itself. To reach something on your machine (a gateway, Ollama,
vLLM, LM Studio), use `http://host.docker.internal:<port>`. The `api` and `worker` services are given that name
(`extra_hosts: host.docker.internal:host-gateway`). Scripts on the host use `localhost`.

### 9Router (local gateway, used for development and the evaluation)

9Router is an OpenAI-compatible gateway on this machine, at `http://localhost:20128/v1`. Its base URL and key are in
the file `.env.local` in the `New_Project` folder, under the names `NINEROUTER_BASE_URL` and `NINEROUTER_API_KEY`. Copy
the key from there; do not paste it into documents or commits.

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=http://host.docker.internal:20128/v1   # from the containers
LLM_API_KEY=<the NINEROUTER_API_KEY value>
LLM_MODEL=cc/claude-sonnet-5-5
```

The evaluation runs on the host and reads the same variables (`backend/eval/run_eval.py`):

```bash
cd backend
uv run python -m eval.run_eval --split dev --model cc/claude-sonnet-5-5
```

`EVAL_BASE_URL` and `EVAL_API_KEY` override the two `NINEROUTER_` variables for the evaluation only. `EVAL_API_KEY` is
optional: an endpoint that takes no key (a local Ollama or LM Studio) needs none. The live tests
(`pytest -m live`) read the `NINEROUTER_` names. Models measured on this gateway are listed under "Measured models".

### Anthropic API

```bash
LLM_PROVIDER=anthropic
LLM_API_KEY=<your Anthropic API key>
LLM_MODEL=<an Anthropic model id>
LLM_PRICE_INPUT_PER_MTOK=<the model's input price>
LLM_PRICE_OUTPUT_PER_MTOK=<the model's output price>
```

The adapter is covered by unit tests against the request and response shapes
(`backend/tests/test_llm_adapters.py::test_anthropic_request_and_response_mapping`). This project's evaluation did
not call the Anthropic API directly; the measured Claude results came through 9Router.

### OpenAI

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=<your OpenAI API key>
LLM_MODEL=<an OpenAI model id that supports tool calling>
```

The protocol is the one exercised through 9Router. The OpenAI endpoint itself was not called in this project.

### Any OpenAI-compatible endpoint (vLLM, LM Studio, LiteLLM, OpenRouter, Groq, Gemini's OpenAI endpoint)

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=<its /v1 URL>                       # for example http://host.docker.internal:8000/v1 for vLLM
LLM_API_KEY=<key, or leave empty if the server takes none>
LLM_MODEL=<the model id that server reports>
```

The base URL must include the `/v1` path; the adapter appends `/chat/completions`. Groq
(`https://api.groq.com/openai/v1`) and Google's OpenAI-compatible endpoint
(`https://generativelanguage.googleapis.com/v1beta/openai`) were not called directly in this project.

### Ollama (a model on your machine)

```bash
LLM_PROVIDER=ollama
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=<a model whose Ollama page lists tool support>
ALLOW_PRIVATE_LLM_URLS=true                      # the default; see below
```

- Pick a model that lists tool support on its Ollama page. A run makes several calls, and each one carries the
  instructions and the tool definitions, so raise the model's context window in Ollama. The server default is too
  small for a long investigation.
- `ALLOW_PRIVATE_LLM_URLS` defaults to `true`, which the local address needs. On a shared installation set it to
  `false`; the address is then refused, and Ollama on the host cannot be used.
- Everything stays on your machine with Ollama. Trace content, which can contain customer data, does not leave it.
- The evaluation can be pointed at Ollama. Run it from the host; no key is needed (set `EVAL_API_KEY` only if the
  endpoint asks for one):

```bash
cd backend
EVAL_BASE_URL=http://localhost:11434/v1 EVAL_API_KEY=ollama \
  uv run python -m eval.run_eval --split dev --model <model name>
```

No local model has been measured for AgentOps. Run the development set before relying on one, and compare it with
the figures in `EVALUATION.md`, which are for hosted models only.

### Validation of base URLs

Every base URL is checked when it is saved and again before a client is built. Only `http` and `https` are accepted,
and a link-local, multicast or unspecified address (including the cloud metadata address `169.254.169.254`) is
refused. Private and loopback addresses are refused only when `ALLOW_PRIVATE_LLM_URLS=false`
(`backend/app/llm/factory.py`, `validate_base_url`).

## Measured models

Results are in `EVALUATION.md`, with the test set run once per model.

| Model (through 9Router) | Held-out test (13 questions) | Development set (27 questions) | Attack suite |
|---|---|---|---|
| `cc/claude-sonnet-5-5` | 8 of 13 passed | 24 of 27 passed | Held all eight cases |
| `ag/gpt-oss-120b-medium` | 3 of 13 passed | not run | Held seven of eight. The first case is inconclusive: its run failed before the planted text was read |
| `cx/gpt-6-luna` | not measured | not measured | Every `cx/` route returned HTTP 503 during the evaluation, including a one-word probe. The attempt is recorded as `unavailable-test-cx-gpt-6-luna.json` and is not a score |

The held-out set is run once per model, after the prompt and tools were fixed on the development set. The regression gate (`backend/eval/baseline.json`, `tests/test_eval_gate.py`) fails when a recorded held-out result
falls more than 0.05 below its baseline.
