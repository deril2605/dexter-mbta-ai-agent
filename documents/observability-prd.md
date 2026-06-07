# Dexter — Observability & Latency Visibility
### Product Requirements Document · Phase 1.5

**Owner:** Deril Raju
**Status:** Approved (first slice in build)
**Scope:** Add per-session tracing and per-stage latency measurement to Dexter,
with **zero added latency** on the request path. Plus a documented latency-mitigation
backlog the data will prioritize.

---

## 1. Summary & context

Phase 1 ships a working MBTA predictions assistant (CLI → FastAPI → LangGraph agent →
LLM-free MBTA library). It works, but we have a **latency concern we have never
measured.** Before optimizing anything we need to *see* where each turn spends its
time and *inspect* what happened in a conversation (slots extracted, routing,
resolution decisions, API calls).

This PRD adds an **observability layer** built on **Arize Phoenix** (open-source,
local) using **OpenInference / OpenTelemetry**. It is a diagnostic, not a cure:
observability **measures** latency; the actual reductions are a separate backlog
(§8) that the measurements will prioritize.

> **Be honest about the gap:** "latency concern" today is a vibe, not a number. The
> first deliverable of this work is replacing the vibe with p50/p95 per stage.

## 2. Goals

- **Per-stage latency**: measure router LLM vs MBTA calls vs resolution, as
  distributions (p50/p95), not single samples.
- **Per-session trace**: every conversation (`session_id` / `thread_id`) viewable
  end-to-end — message → slots → routing → resolution → API calls → reply.
- **Correctness visibility**: see the router's extracted slots and the LLM I/O, and
  the resolution outcome (route/stop/direction, candidates).
- **Zero added latency**: instrumentation must not slow the user-facing request.
- **Opt-in & local**: off by default; when on, data stays on the machine.

## 3. Non-goals

- Inline LLM-as-judge / online evals (these *add* latency and cost).
- Multi-user, sampling, retention, or production-grade collector infra — single
  local user for now.
- Replacing logs/metrics dashboards — this is tracing for development insight.

## 4. Latency analysis (hypothesis to confirm)

Per-turn path and expected cost:

| Stage | Work | Expected cost |
|---|---|---|
| FastAPI in/out | local HTTP | negligible |
| **router_node** | **one `gpt-5-mini` call** | **dominant** |
| resolution | route cache (mem) + `/stops` (cached 6h) | ~0 after warm; ~100–300ms first time |
| predictions | `/predictions` (never cached) | ~100–300ms |
| schedules | only on fallback | ~100–300ms |
| format | templates | negligible |

**Hypothesis: the router LLM call ≈ most of the wall-clock.** `gpt-5-mini` is a
*reasoning* model and `dexter/agent/router.py` sets `max_completion_tokens=2000`,
which permits a large hidden reasoning budget for what is a trivial slot-extraction
task. The first job of this layer is to confirm or refute that with real spans
(including token counts).

## 5. Requirements

- **Async export.** Spans leave the process via an OpenTelemetry `BatchSpanProcessor`
  on a background thread (OTLP → Phoenix). The request path only pays an in-memory
  append. No synchronous exporters on the hot path.
- **Opt-in gate.** Controlled by `DEXTER_TRACING` (default `false`). When off, the
  global OTel tracer is a no-op → instrumentation is ~free and the tracing
  dependencies are not imported.
- **Secret redaction (hard requirement).** Never record the MBTA `X-API-Key` or the
  Azure OpenAI key. Manual spans attach only safe attributes (endpoint path, status,
  cache hit) — never request headers.
- **Session grouping.** Each turn is tagged with its `session_id` so Phoenix groups a
  conversation.
- **No behavior change.** Tracing is passive; the agent's outputs are identical
  whether tracing is on or off.

## 6. Design

**Stack:** Arize Phoenix (local UI + OTLP collector) + OpenInference auto-instrumentors
+ OpenTelemetry SDK.

**Instrumentation points:**
- **LangGraph nodes** — captured automatically by
  `openinference-instrumentation-langchain` (LangGraph runs on langchain-core
  Runnables). Node inputs/outputs (state: slots, results) appear as span attributes.
- **AzureOpenAI router call** — captured automatically by
  `openinference-instrumentation-openai`, including prompt, tool call, model, and
  **token counts + latency**. No code change in `router.py`.
- **MBTA API calls** — a **manual span** at the single choke point
  `MBTAClient.get_json` (all `/routes`, `/stops`, `/predictions`, `/schedules` flow
  through it). Attributes: `http.route` (path), `http.status_code`, `cache_hit`.
- **Session** — wrap each `/chat` turn in OpenInference `using_session(session_id)`.

**Wiring:** a new `dexter/observability.py` exposes `configure_tracing(settings)`
(idempotent; no-op unless `DEXTER_TRACING`), a `session(session_id)` context manager,
and a module `tracer`. The service calls `configure_tracing` on startup and wraps the
chat turn in `session(...)`.

## 7. First slice vs later

**First slice (now):** the design in §6 — opt-in Phoenix/OTel setup, auto-instrumented
graph + LLM, manual MBTA spans, session tagging, redaction, an `InMemorySpanExporter`
unit test, and a README "Observability (optional)" section.

**Later:** explicit resolution-decision spans (candidate scores, why a disambiguation
fired), saved p50/p95 views, and a lightweight CI perf-smoke check.

## 8. Latency-mitigation backlog (NOT built here — prioritized by the data)

1. **Reconsider the router model.** `gpt-5-mini`'s reasoning is likely overkill for
   slot extraction. A *non-reasoning* `gpt-4o-mini` / `gpt-4.1-mini` may be 2–5×
   faster at equal quality — revisiting our earlier "best model" pick toward "fastest
   adequate."
2. **Cap reasoning.** Lower `max_completion_tokens`; set a minimal `reasoning_effort`
   for gpt-5 models.
3. **Skip the LLM on disambiguation-answer turns.** `clarify` already matches answers
   to options deterministically (rapidfuzz), so when a clarification is pending we can
   resolve a short answer ("Forest Hills", "the 116") **without a router call** —
   removing the dominant cost from multi-turn flows. (Fall back to the LLM only if no
   option matches.)
4. **Streaming is low value here** — the wait is LLM + MBTA; the reply text is
   templated and instant. Skip.

## 9. Privacy & security

- MBTA and Azure keys are **redacted** (never attached to spans).
- User messages and resolved stop names reveal a user's *location intent*; they are
  low sensitivity for a single local user but should not be shipped off-machine —
  hence local Phoenix, opt-in, off by default.

## 10. Open questions

- Once measured: is the router truly the bottleneck, and is the answer model-swap
  (§8.1) or LLM-skip (§8.3) — or both?
- Do we want a tiny always-on, in-process latency counter (cheap, no Phoenix) for a
  quick p50/p95 readout, independent of the full tracing UI?
