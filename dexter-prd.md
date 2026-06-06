# Dexter — MBTA Transit Assistant
### Product Requirements Document · Phase 1

**Owner:** Deril Raju
**Status:** Ready for build (hand to Claude Code)
**Scope of this doc:** Phase 1 only, with an architecture that admits later phases without rewrites.

---

## 1. Summary

Dexter is a natural-language assistant for MBTA (Boston) transit questions. A user asks, in plain English, things like *"when's the next 116 from Bennington Street toward Maverick?"* and gets a short, speakable answer grounded in the MBTA V3 API.

Phase 1 ships a **text** assistant (CLI client → local service). Later phases add more skills (alerts, facilities), then voice and a Raspberry Pi thin client. The architecture is layered so each later phase is an *addition*, not a rewrite.

**Phase 1 delivers one skill end-to-end: real-time predictions ("next bus/train") with a schedule fallback.** Everything else is scaffolded but not implemented.

---

## 2. Goals & Non-Goals

### Goals
- Answer "next vehicle" questions in natural language for any MBTA route + stop + direction.
- Resolve fuzzy human phrasing ("116", "Blue Line", "Bennington St", "toward Maverick") to MBTA GTFS identifiers reliably.
- Multi-turn conversation: follow-ups ("and the one after?") and clarifying questions both work.
- Schedule fallback when real-time data is empty; clear "no service" message when nothing is scheduled.
- Every response is short and **TTS-speakable** (no raw IDs, no JSON).
- Clean separation between MBTA logic, the agent, and the interface — so adding skills or a voice client is additive.

### Non-Goals (Phase 1)
- Voice / wake word ("Hey Dexter") — later phase.
- Service alerts ("is the Blue Line down?") — scaffolded only.
- Facility status (elevators/escalators) — scaffolded only.
- Trip planning (A→B routing) — **explicitly out**; the V3 API does not do multi-modal routing and it would need OpenTripPlanner. Not in any near-term phase.
- MCP server — **not built unless a real consumer needs it.** Domain logic is a library so MCP can wrap it later with zero refactor.
- Auth / multi-user — single local user.

---

## 3. Architecture

Three layers, strictly separated. Dependencies point downward only.

```
┌─────────────────────────────────────────────┐
│  Interface layer                             │
│  - CLI REPL client (Phase 1)                 │
│  - [later] voice client / Pi thin client     │
└───────────────┬─────────────────────────────┘
                │ HTTP (JSON)
┌───────────────▼─────────────────────────────┐
│  Brain — FastAPI service                     │
│  - POST /chat  (session_id, message)         │
│  - LangGraph agent (router → skill → format) │
│  - conversation state via checkpointer       │
└───────────────┬─────────────────────────────┘
                │ in-process function calls
┌───────────────▼─────────────────────────────┐
│  MBTA core library  (no LLM, no agent)       │
│  - resolution (route → stop → direction)     │
│  - predictions, schedules                    │
│  - response data objects (speakable-ready)   │
│  - caching                                   │
└──────────────────────────────────────────────┘
```

**Why service-from-day-one (your choice #2 = option 1):** the CLI is the *first client* of the brain over HTTP. When the Pi arrives, it becomes just another client hitting the same `/chat` endpoint. No re-plumbing.

**Why the MBTA library is LLM-free:** it's the in-process "hot path." The agent calls it directly (low latency, no extra hop). If we ever want MCP, we wrap this library — the library doesn't change.

---

## 4. Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Agent | LangGraph (router + skill nodes + checkpointer for multi-turn) |
| LLM | Azure OpenAI via `openai` SDK (deployment names from `.env`) |
| Service | FastAPI + uvicorn |
| HTTP client | `httpx` (async) |
| Fuzzy matching | `rapidfuzz` |
| Caching | in-memory TTL cache (e.g. `cachetools`); SQLite optional, not required Phase 1 |
| Config | `pydantic-settings` reading `.env` |
| Tests | `pytest` + `respx` (mock MBTA HTTP) |
| Packaging | `uv` or `pip` + `pyproject.toml` |

**Model usage:** a small/fast deployment (e.g. `gpt-4o-mini`-class) handles intent + slot extraction via tool-calling. **Departure times are NEVER produced by the LLM** — they come from the API and are template-formatted, to eliminate hallucinated times. The LLM extracts slots and (optionally) naturalizes phrasing using only data we pass it.

---

## 5. MBTA Core Library — detailed spec

### 5.1 Client basics
- Base URL: `https://api-v3.mbta.com`
- Auth: `X-API-Key` header from `.env`. (Key raises limit to 1000 req/min.)
- JSON:API format. Use `filter[...]`, `include`, and sparse `fields[...]` to minimize payload.
- Respect `Last-Modified` / `If-Modified-Since` where practical.

### 5.2 Route cache
- On startup (and refresh daily): `GET /routes`.
- Store per route: `id`, `short_name`, `long_name`, `type` (0–4), `direction_names`, `direction_destinations`.
- Used for: route-name → `route_id`, and direction resolution.

### 5.3 Resolution algorithm (route-first)

This is the core of the project. Resolve in this order:

**Step 1 — Route.** Extract the route token from the parsed slots ("116", "Blue Line", "Green Line B").
- Bus: match token against `short_name` (e.g. `"116"` → `route_id "116"`).
- Subway/CR: match against `long_name`/`short_name` (e.g. "Blue Line" → `"Blue"`).
- No confident match → ask a clarifying question (see disambiguation).

**Step 2 — Stop (scoped to the route).** Once `route_id` is known:
- `GET /stops?filter[route]={route_id}` → small set (~30–60 stops).
- `rapidfuzz` the user's location phrase against stop `name`s within that set only.
- Single clear winner → use its `stop_id`.
- Multiple close matches or none above threshold → disambiguation.

> Scoping the fuzzy match to one route is what makes resolution accurate instead of guessing across thousands of stops.

**Step 3 — Direction.** From the route's `direction_destinations` (route-specific!):
- Match "toward Maverick" against `direction_destinations[i]` → `direction_id = i`.
- **Never hardcode 0/1** — its meaning differs per route.
- If user gave no direction and both exist → **ask which direction of travel** (disambiguation), using the route's `direction_destinations` for the options (e.g. *"Which direction — toward Maverick or toward Harvard?"*). Resolve on the next turn.

### 5.4 Predictions
- `GET /predictions?filter[stop]={stop_id}&filter[route]={route_id}&filter[direction_id]={d}&include=trip&sort=departure_time`
- A filter is mandatory or the endpoint returns nothing.
- Extract next 2–3 `departure_time` (fall back to `arrival_time` at terminals).
- Convert to **relative minutes from now**.

### 5.5 Schedule fallback (your choice #1)
When predictions are empty:
1. `GET /schedules?filter[stop]=...&filter[route]=...&filter[direction_id]=...&filter[date]=today&sort=departure_time`, take the next scheduled time after now.
   - → *"Real-time data isn't available, but per the schedule the next 116 toward Maverick should come around 11:42 PM."*
2. If no remaining scheduled service today:
   - → *"There appear to be no 116 buses toward Maverick around you right now."*

### 5.6 Return objects
Library returns typed dataclasses (e.g. `PredictionResult`, `ScheduleResult`, `NoServiceResult`, `Disambiguation`) — **structured, not strings.** Formatting to speakable text happens in the agent layer so the same data can later feed a voice client.

### 5.7 Caching
- `/routes`: TTL 24h.
- per-route `/stops`: TTL ~6h.
- predictions/schedules: **never cache** (real-time).

---

## 6. Agent Layer — LangGraph

### 6.1 Graph shape
```
START
  → router_node        (classify intent)
  → [predictions_node | alerts_node* | facilities_node* | clarify_node | fallback_node]
  → format_node        (structured result → speakable text)
  → END
```
`*` = scaffolded stub returning "not available yet" in Phase 1.

### 6.2 router_node (multi-skill from day one)
Even though only predictions is implemented, the router classifies into `predictions | alerts | facilities | unknown` and extracts slots. This is cheap insurance so later skills slot in without restructuring.

LLM tool-calling extracts slots:
```json
{
  "intent": "predictions",
  "route": "116",
  "location": "Bennington Street",
  "direction_hint": "Maverick",
  "follow_up": false
}
```

### 6.3 Multi-turn state (your choice #5)
- Use a LangGraph **checkpointer** (`MemorySaver` for Phase 1) keyed by `session_id`.
- State carries last resolved `{route_id, stop_id, direction_id}` and any `pending_disambiguation`.
- "and the one after?" / "what about inbound?" reuse prior slots.

### 6.4 Disambiguation (your choice #3 = yes)
When resolution is ambiguous, `clarify_node` returns a question and stores candidates in state:
- *"Did you mean Maverick Station, or Bennington St @ Brooks St?"*
- The next user turn resolves against the stored candidates.

### 6.5 format_node (speakable output)
- Templates turn structured results into short spoken lines:
  - *"The next 116 toward Maverick is in 4 minutes, then 12 and 19 minutes."*
- No stop IDs, no JSON, no times invented by the LLM.

---

## 7. Service & Interface

### 7.1 FastAPI brain
- `POST /chat` → body `{ "session_id": str, "message": str }` → `{ "reply": str, "needs_input": bool }`
- `GET /health`
- Loads route cache on startup.

### 7.2 CLI REPL client (Phase 1 interface)
- Generates a `session_id`, loops on stdin, prints `reply`.
- Thin: only talks to `/chat`. No logic lives here (so the Pi can replace it cleanly).

---

## 8. Configuration (`.env`)

Provide a `.env.example`; real values filled in by Deril (your choice #4).

```
# MBTA
MBTA_API_KEY=
MBTA_BASE_URL=https://api-v3.mbta.com

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_API_VERSION=
AZURE_OPENAI_DEPLOYMENT_ROUTER=     # small/fast model for intent + slots

# Service
DEXTER_HOST=127.0.0.1
DEXTER_PORT=8000
LOG_LEVEL=INFO
```
Config loaded via `pydantic-settings`. No secrets committed.

---

## 9. Project Structure

```
dexter/
├── pyproject.toml
├── .env.example
├── README.md
├── dexter/
│   ├── config.py                 # pydantic-settings
│   ├── mbta/
│   │   ├── client.py             # httpx wrapper, auth, caching
│   │   ├── routes.py             # route cache
│   │   ├── resolution.py         # route → stop → direction
│   │   ├── predictions.py
│   │   ├── schedules.py          # fallback
│   │   └── models.py             # dataclasses (PredictionResult, etc.)
│   ├── agent/
│   │   ├── graph.py              # LangGraph wiring
│   │   ├── router.py             # intent + slot extraction (LLM)
│   │   ├── nodes.py              # predictions / clarify / fallback / stubs
│   │   ├── formatting.py         # structured → speakable
│   │   └── state.py              # conversation state schema
│   ├── service/
│   │   └── app.py                # FastAPI
│   └── cli/
│       └── repl.py               # Phase-1 client
└── tests/
    ├── test_resolution.py
    ├── test_predictions.py
    ├── test_schedules_fallback.py
    └── test_agent_multiturn.py
```

---

## 10. Example Interactions (acceptance scenarios)

1. **Basic prediction**
   - *"when's the next 116 from Bennington Street toward Maverick?"*
   - → *"The next 116 toward Maverick is in 4 minutes, then 12 minutes."*
2. **Follow-up (multi-turn)**
   - *"and the one after that?"* → reuses route/stop/direction, returns later departures.
3. **Schedule fallback**
   - (no real-time data) → *"Real-time data isn't available, but per the schedule the next one should come around 11:42 PM."*
4. **No service**
   - → *"There appear to be no 116 buses toward Maverick around you right now."*
5. **Disambiguation**
   - *"next bus from Bennington Street"* (route unclear / stop ambiguous) → *"Which route — the 116 or the 117?"* → resolves on next turn.
6. **Scaffolded skill**
   - *"is the Blue Line down?"* → *"I can't check service alerts yet — that's coming in a later version."* (proves the router branches correctly.)

---

## 11. Error Handling & Edge Cases

- MBTA 429 (rate limit): brief backoff + "the transit feed is busy, try again in a moment."
- MBTA timeout / 5xx: graceful "I couldn't reach the MBTA feed right now."
- Unknown route: clarify, don't guess.
- Direction omitted, both exist: ask which direction of travel (offer the two destinations), resolve next turn.
- Terminal stops: use `arrival_time` when `departure_time` is null.
- Times: always relative ("in 4 minutes"); "now"/"arriving" for <1 min.

---

## 12. Definition of Done (Phase 1)

- [ ] Scenarios 1–6 in §10 pass against the live API (with key).
- [ ] Resolution scoped route-first; direction resolved from `direction_destinations`.
- [ ] Multi-turn follow-ups and disambiguation both work via the checkpointer.
- [ ] Schedule fallback + no-service phrasing match §5.5.
- [ ] All responses speakable (no IDs/JSON; no LLM-generated times).
- [ ] Brain is a FastAPI service; CLI is a thin client over `/chat`.
- [ ] Config entirely via `.env`; `.env.example` present; no secrets committed.
- [ ] Unit tests with mocked MBTA HTTP for resolution, predictions, fallback, multi-turn.

---

## 13. Later Phases (scaffolded, not built)

- **1.5 — Alerts & facilities skills.** Implement the stubbed router branches. Map alert `severity`/`effect` to plain language ("suspended" vs "minor delays"). Facilities = elevator/escalator outages.
- **2 — "Should I leave now?"** Combine a saved home stop + fixed walk time with predictions.
- **2 — Presets / favorites.** "my usual" → saved route+stop.
- **3 — Voice & Raspberry Pi thin client.** Pi does audio capture, wake word ("Hey Dexter"), STT (Azure Speech / Whisper), and TTS playback; the **brain stays in the cloud**. Pi calls the same `/chat`. No agent logic on the Pi.
- **If-needed — MCP server.** Wrap the MBTA core library as an MCP server *only* if a real external consumer (Claude Desktop/Code, another client) needs it. No change to the library.

---

## 14. Open Assumptions (flag if wrong)

- Single user, local, no auth in Phase 1.
- Azure OpenAI has at least one chat-capable deployment Deril will name in `.env`.
- "Around you" phrasing in no-service messages refers to the resolved stop, not GPS (no geolocation in Phase 1).
- When no direction is specified and both exist, Dexter asks which direction of travel (offering the two destinations) rather than guessing or returning both.
