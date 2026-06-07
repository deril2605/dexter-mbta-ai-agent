# Dexter — MBTA Transit Assistant

A natural-language assistant for MBTA (Boston) transit questions. Ask, in plain
English — *"when's the next 116 from Bennington Street toward Maverick?"* — and
get a short, speakable answer grounded in the MBTA V3 API.

**Phase 1 (this repo)** delivers one skill end-to-end: real-time **predictions**
("next bus/train") with a **schedule fallback**, over a text interface. Alerts and
facilities are scaffolded (the router classifies them, but they reply "coming in a
later version").

See **`documents/dexter-prd.md`** for the full spec (source of truth), **`CLAUDE.md`**
for the architecture invariants, and **`documents/BUILD_PLAN.md`** for the milestone
plan.

## Architecture

Three layers, dependencies point **downward only**:

```
CLI REPL client                          interface
      │  HTTP (POST /chat)
      ▼
FastAPI brain  ──  LangGraph agent        brain
      │            router → skill → format (+ MemorySaver checkpointer)
      ▼
MBTA core library (LLM-free)             library
      resolution · predictions · schedules · caching
```

Key invariants (enforced throughout):

- **The MBTA library is LLM-free** — the fast in-process path; no OpenAI calls in
  `dexter/mbta/`.
- **The LLM never produces times.** `gpt-5-mini` extracts intent + slots only;
  every time is templated from API data in `agent/formatting.py`. This eliminates
  hallucinated departures.
- **Resolution is route-first:** resolve the route, then fetch only *that route's*
  stops and fuzzy-match within them. Never fuzzy-match across all stops.
- **Direction comes from the route's `direction_destinations`** — never a
  hardcoded `direction_id` 0/1.
- **Every reply is speakable:** no stop/route IDs, no JSON; relative times
  ("in 4 minutes").

## Setup

Requires Python 3.11+.

```bash
# install (uv)
uv sync --extra dev
# or with pip
pip install -e ".[dev]"

# configure
cp .env.example .env   # then fill in real values (see below)
```

### Configuration (`.env`)

| Variable | Required | Notes |
|---|---|---|
| `MBTA_API_KEY` | optional | Raises the rate limit to 1000 req/min; the API works without it. |
| `MBTA_BASE_URL` | — | Defaults to `https://api-v3.mbta.com`. |
| `AZURE_OPENAI_ENDPOINT` | **yes** | e.g. `https://<resource>.openai.azure.com/` or the Foundry `.cognitiveservices.azure.com/` endpoint. |
| `AZURE_OPENAI_API_KEY` | **yes** | From the resource's *Keys and Endpoint*. |
| `AZURE_OPENAI_API_VERSION` | **yes** | Copy from the deployment's *View code* panel (e.g. `2025-04-01-preview`). |
| `AZURE_OPENAI_DEPLOYMENT_ROUTER` | **yes** | The **deployment name** of a small/fast chat model (default: `gpt-5-mini`). |
| `DEXTER_HOST` / `DEXTER_PORT` | — | Service bind address (defaults `127.0.0.1:8000`). |
| `LOG_LEVEL` | — | Defaults `INFO`. |

`.env` is gitignored — never commit secrets. The MBTA library and its tests run
without any credentials; Azure values are only needed to run the agent/service.

## Run

```bash
# brain (terminal 1)
uvicorn dexter.service.app:app --reload --port 8000

# CLI client (terminal 2)
python -m dexter.cli.repl
```

The CLI reads the service URL from `DEXTER_URL` (or `DEXTER_HOST`/`DEXTER_PORT`),
defaulting to `http://127.0.0.1:8000`.

Endpoints:
- `POST /chat` — `{ "session_id": str, "message": str }` → `{ "reply": str, "needs_input": bool }`
- `GET /health` — `{ "status": "ok" }`

## Develop

```bash
pytest                      # unit tests (MBTA HTTP + LLM are mocked; no network)
ruff check . && ruff format .
```

Tests mock MBTA HTTP with `respx` and fake the LLM — the suite never hits the live
API. Coverage spans route-first resolution, direction-from-destinations,
predictions + schedule fallback + no-service, the router, the nodes, speakable
formatting, multi-turn follow-ups and disambiguation, and the service.

## Observability (optional)

Dexter can emit OpenTelemetry traces to a local **Arize Phoenix** instance to inspect
each conversation and measure where time goes (router LLM vs MBTA calls). It's
**off by default** and adds no latency when disabled. See
**`documents/observability-prd.md`**.

```bash
uv sync --extra tracing       # install Phoenix + OpenInference instrumentors
phoenix serve                 # local UI at http://localhost:6006 (separate terminal)

# enable tracing for the service
DEXTER_TRACING=true uvicorn dexter.service.app:app --port 8000
```

Then open `http://localhost:6006` — traces are grouped by `session_id`, with spans
for the LangGraph nodes, the `gpt-5-mini` router call (token counts + latency), and
each MBTA API call. MBTA/Azure keys are never recorded.

## Project layout

```
dexter/
├── config.py              # pydantic-settings (lazy get_settings)
├── mbta/                  # LLM-free core library
│   ├── client.py          # async httpx wrapper, auth, TTL cache, typed errors
│   ├── routes.py          # route cache + name lookup
│   ├── resolution.py      # route → stop → direction (route-first)
│   ├── predictions.py     # predictions + departures orchestration
│   ├── schedules.py       # schedule fallback
│   ├── models.py          # typed dataclasses (results, disambiguation)
│   └── _timeutils.py      # MBTA time parsing / service date
├── agent/
│   ├── router.py          # Azure gpt-5-mini tool-calling (intent + slots)
│   ├── nodes.py           # predictions / clarify / stubs / fallback
│   ├── formatting.py      # structured result → speakable text
│   ├── graph.py           # LangGraph wiring + MemorySaver checkpointer
│   └── state.py           # conversation state + outcome types
├── service/app.py         # FastAPI brain
└── cli/repl.py            # thin CLI client
```

## Known limitations (Phase 1)

- **Alerts & facilities are stubs** — the router classifies them correctly and
  Dexter replies that they're coming in a later version (PRD §13).
- **No route given → generic clarification.** Asking "next bus from Bennington
  Street" (no route) returns *"Which route did you mean?"* rather than naming
  candidate routes, to preserve the route-first invariant (see PRD scenario 5;
  deliberate decision).
- **Follow-ups reuse the resolved target** and return the current upcoming
  departures. Precise "the one *after* that" offset semantics is a later-phase
  refinement — the router's slot schema (PRD §6.2) carries no offset.
- **Single local user, in-memory conversation state** (`MemorySaver`); state is
  not persisted across service restarts.
