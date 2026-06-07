# BUILD_PLAN.md — Dexter Phase 1

A bottom-up, milestone-by-milestone plan for Claude Code. Build in this order; **stop and verify at each milestone** before moving up a layer. The PRD (`dexter-prd.md`) is the source of truth for behavior; `CLAUDE.md` for conventions.

---

## Kickoff prompt (paste this as your first message to Claude Code)

> You're building **Dexter**, a natural-language MBTA transit assistant. Read `dexter-prd.md` (full spec, source of truth), `CLAUDE.md` (conventions and architecture invariants), and this `BUILD_PLAN.md`.
>
> Build Phase 1 only, bottom-up, one milestone at a time in the order below. After each milestone: write its tests, run them, and report what you built and how you verified it before starting the next one. Do not run through multiple milestones silently. Respect the architecture invariants in `CLAUDE.md` strictly — especially: the MBTA library is LLM-free, the LLM never generates departure times, resolution is route-first, and direction comes from `direction_destinations`. If anything in the PRD is ambiguous or looks wrong, ask me before building it.
>
> Start with Milestone 0.

---

## Milestone 0 — Skeleton & config
- `pyproject.toml`, package layout per PRD §9, `.env.example` per PRD §8, `README.md` stub.
- `dexter/config.py` with `pydantic-settings` loading all `.env` values.
- **Verify:** package imports; config loads from a sample `.env`; missing required vars raise a clear error.

## Milestone 1 — MBTA client + route cache
- `dexter/mbta/client.py`: async `httpx` wrapper, `X-API-Key` header, base URL from config, JSON:API helpers, error mapping (429 / timeout / 5xx), TTL cache.
- `dexter/mbta/routes.py`: load + cache `/routes`; expose route lookup by short/long name and access to `direction_names` / `direction_destinations`.
- **Verify (respx-mocked):** route lookup resolves "116" → `"116"` and "Blue Line" → `"Blue"`; cache TTL honored; 429/timeout surface as typed errors.

## Milestone 2 — Resolution (the core)
- `dexter/mbta/resolution.py`: route-first algorithm (PRD §5.3).
  - route → fetch `/stops?filter[route]=...` → `rapidfuzz` within that set → `stop_id`.
  - direction from `direction_destinations`; missing direction with two options → return a `Disambiguation`.
  - ambiguous/low-confidence stop → `Disambiguation`.
- `dexter/mbta/models.py`: dataclasses (`PredictionResult`, `ScheduleResult`, `NoServiceResult`, `Disambiguation`, resolution types).
- **Verify (mocked):** known stop resolves; ambiguous stop yields candidates; missing direction yields a direction question; bad route asks for clarification. **This is the highest-risk milestone — test it hard.**

## Milestone 3 — Predictions + schedule fallback
- `dexter/mbta/predictions.py`: `/predictions` with mandatory filters, next 2–3 departures, relative-minute conversion, terminal `arrival_time` fallback.
- `dexter/mbta/schedules.py`: empty-predictions fallback → next scheduled time, else no-service result (PRD §5.5 phrasing).
- **Verify (mocked):** populated predictions return correct relative times; empty predictions trigger schedule fallback; no remaining service returns the no-service result.

## Milestone 4 — Agent state, router, nodes
- `dexter/agent/state.py`: conversation state (last `{route_id, stop_id, direction_id}`, `pending_disambiguation`).
- `dexter/agent/router.py`: Azure OpenAI tool-calling for intent + slot extraction (PRD §6.2); classifies `predictions | alerts | facilities | unknown`.
- `dexter/agent/nodes.py`: `predictions_node` (calls the library), `clarify_node`, `fallback_node`, and **stub** `alerts_node` / `facilities_node` ("coming in a later version").
- **Verify:** router returns correct intent + slots on sample utterances (mock the LLM); stub branches reachable.

## Milestone 5 — Formatting + graph wiring
- `dexter/agent/formatting.py`: structured results → speakable strings (PRD §6.5). **Times templated, never LLM-generated.**
- `dexter/agent/graph.py`: wire router → skill node → format, with `MemorySaver` checkpointer keyed by `session_id` (multi-turn + disambiguation resolution).
- **Verify:** multi-turn follow-up ("and the one after?") reuses slots; a disambiguation answer on the next turn resolves correctly; no IDs/JSON in any output.

## Milestone 6 — Service + CLI
- `dexter/service/app.py`: FastAPI `POST /chat` ({session_id, message} → {reply, needs_input}), `GET /health`, route cache loaded on startup.
- `dexter/cli/repl.py`: thin client — generates `session_id`, loops stdin, calls `/chat`, prints `reply`. No logic.
- **Verify:** end-to-end against the **live** API (with key) — run all six scenarios in PRD §10.

## Milestone 7 — Hardening & DoD
- Fill test gaps; confirm the PRD §12 checklist; tidy `README.md` (setup, `.env`, run instructions).
- **Verify:** `pytest` green; `ruff` clean; all §10 scenarios pass live; no secrets committed.

---

## Sequencing notes
- Milestones 1–3 (the LLM-free library) can be fully built and tested with **zero LLM calls** — get this rock-solid first; it's the foundation and the cheapest to test.
- Only Milestones 4–5 introduce Azure OpenAI. Mock the LLM in unit tests; reserve live LLM + live MBTA for the Milestone 6 end-to-end check.
- Keep the alerts/facilities branches as stubs throughout — proving the router *routes* them is the Phase 1 goal, not implementing them.
