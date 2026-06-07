# CLAUDE.md — Dexter

Project guidance for Claude Code. Read this every session. The full spec is in **`documents/dexter-prd.md`** — that PRD is the source of truth; this file is the working agreement.

## What we're building (Phase 1)
A natural-language MBTA transit assistant. Real-time **predictions** ("next bus/train") with a schedule fallback are the core flow, and **alerts** plus **facilities** queries are also implemented. Text only. CLI client → FastAPI brain → MBTA core library.

## Architecture invariants (do not violate)
1. **Three layers, dependencies point DOWN only:** interface → brain (FastAPI + LangGraph) → MBTA core library. The library never imports from the agent or service.
2. **The MBTA core library is LLM-free.** No OpenAI calls in `dexter/mbta/`. It's the fast in-process path and the future MCP-wrap target.
3. **The LLM never produces departure times.** It extracts intent + slots only. Times come from the API and are template-formatted in `agent/formatting.py`. This is non-negotiable — it prevents hallucinated times.
4. **No logic in the CLI.** `cli/repl.py` only calls the `/chat` HTTP endpoint. The Pi will later replace it, so it must stay dumb.
5. **Library returns typed dataclasses, not strings.** Formatting to speakable text happens in the agent layer.
6. **Every user-facing response is speakable:** no stop IDs, no route IDs, no JSON. Relative times ("in 4 minutes").

## Stack
- Python 3.11+, `uv` (or pip) + `pyproject.toml`
- LangGraph (router + skill nodes + `MemorySaver` checkpointer for multi-turn)
- Azure OpenAI via `openai` SDK
- FastAPI + uvicorn
- `httpx` (async) for MBTA
- `rapidfuzz` for stop matching
- `pydantic-settings` for config
- `pytest` + `respx` for tests

## Conventions
- Async throughout the MBTA client and service.
- All config via `pydantic-settings` reading `.env`. **Never** hardcode keys, deployment names, or URLs. Keep `.env.example` current; never commit secrets.
- Resolution is **route-first**: resolve the route, then fetch only that route's stops (`/stops?filter[route]=...`) and fuzzy-match within that small set. Never fuzzy-match across all stops.
- Direction is resolved from the route's `direction_destinations` — **never hardcode `direction_id` 0/1**.
- Cache `/routes` (24h TTL) and per-route stops (~6h TTL). **Never cache predictions or schedules.**

## Commands
```bash
# install
uv sync                        # or: pip install -e .

# run the brain
uvicorn dexter.service.app:app --reload --port 8000

# run the CLI client (separate terminal)
python -m dexter.cli.repl

# tests / lint
pytest
ruff check . && ruff format .
```

## Testing rules
- Unit tests **mock MBTA HTTP with `respx`** — never hit the live API in tests.
- Cover: route-first resolution, direction resolution from `direction_destinations`, predictions formatting, schedule fallback + no-service phrasing, multi-turn follow-up, missing-direction disambiguation.
- Each fix gets a regression test.

## Out of scope for Phase 1 (do NOT build)
- MCP server (library stays wrappable; don't wrap it yet).
- Voice / wake word / Pi client.
- Trip planning (A→B routing).
- New skills beyond predictions, alerts, and facilities unless the scope is explicitly expanded.

## Definition of done
The six scenarios in PRD §10 pass against the live API (with key), and the §12 checklist is complete. Don't consider a phase finished until its tests are green and the scenarios verified.

## Working style
- Build bottom-up (see `documents/BUILD_PLAN.md`). Finish and test each layer before moving up.
- After each milestone, stop and report what was built + how you verified it. Don't run ahead through multiple milestones silently.
- If something in the PRD is ambiguous or seems wrong, flag it and ask — don't guess and build.
