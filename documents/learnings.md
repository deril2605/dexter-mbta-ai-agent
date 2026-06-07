# Dexter — Learnings & Decisions Log

A chronological record of the meaningful decisions, course-corrections, and lessons
on this project — *what* we changed, *why*, and *what it bought us*. The goal is to
show how the project actually progressed (the reasoning and the trade-offs), not just
where it landed.

**How to read it:** newest at the bottom. Each entry is short — Decision / Why /
Outcome (and the Lesson when there is one). This log is maintained as we go.

---

## Phase 1 — Building the predictions assistant (2026-06-06)

### 1. Three layers, dependencies point down only
**Decision:** CLI client → FastAPI brain (LangGraph agent) → an **LLM-free** MBTA core
library. The library never imports from the agent or service.
**Why:** keep MBTA logic on a fast in-process path, independently testable, and
wrappable as an MCP server later with no rewrite. The agent orchestrates; the library
just resolves and fetches.
**Outcome:** the entire library (M1–M3) was built and tested with **zero LLM calls and
zero credentials** — the cheapest, most reliable part of the system to get right first.

### 2. Lazy config, required-only-where-needed
**Decision:** expose settings via a cached `get_settings()` rather than instantiating
config at import time; make Azure creds required but MBTA/service values
optional/defaulted.
**Why:** if config validated at import, importing the LLM-free library would demand
Azure creds it never uses — coupling the fast path to the LLM.
**Lesson:** *where* you validate config is an architecture decision, not a detail.

### 3. Route-first resolution (the core algorithm)
**Decision:** resolve the **route** first, then fetch only **that route's** stops and
fuzzy-match the location *within that small set*. Never fuzzy-match across all ~10k MBTA
stops. Direction always comes from the route's `direction_destinations`, never a
hardcoded `direction_id` 0/1.
**Why:** scoping the fuzzy match to one route is what makes resolution accurate instead
of guessing; and direction id meaning differs per route.
**Outcome:** clean, accurate resolution and small payloads.

### 4. The LLM never produces departure times
**Decision:** the LLM extracts intent + slots only; every time is templated from API
data in `agent/formatting.py`.
**Why:** a model that writes "in 4 minutes" will eventually hallucinate "in 4 minutes."
Removing it from the answer path makes wrong times structurally impossible.
**Lesson:** put the LLM where it's good (messy language → structured intent) and keep it
away from facts it shouldn't invent.

### 5. Multi-turn via a checkpointer, disambiguation by slot-accumulation
**Decision:** LangGraph `MemorySaver` keyed by `session_id`; clarifying questions store
the slots that triggered them and the next turn fills the missing slot and re-resolves.
**Why:** follow-ups ("and the one after?") and clarifications ("toward Maverick") should
reuse context without bespoke per-case logic.
**Outcome:** one uniform mechanism handles follow-ups and disambiguation.

### 6. Scenario-5 trade-off: protect the invariant over the literal spec
**Decision:** when no route is given ("next bus from Bennington Street"), ask a generic
"Which route?" instead of inferring candidate routes from the stop — even though the PRD
example named "the 116 or the 117."
**Why:** naming candidates would require inferring route *from* stop, which breaks the
route-first invariant. We chose the invariant and flagged the deviation.
**Lesson:** when a spec example conflicts with a core invariant, surface it and decide
deliberately — don't quietly break the invariant to match wording.

### 7. First model pick: gpt-5-mini
**Decision:** route via Azure `gpt-5-mini` (Chat Completions + tool-calling).
**Why:** strong tool-calling, "best available small model," validated with a live smoke
test.
**Note:** this looked right on quality grounds. It was later revisited on *latency*
grounds (see #11–#12) — a good example of a reasonable decision that data overturned.

---

## Phase 1 — Hardening from live testing

### 8. The disambiguation infinite loop (a real bug, found live)
**Symptom:** asking for a stop on the 39 looped forever — "Which stop did you mean — S
Huntington Ave at Huntington Ave, S Huntington Ave at Huntington Ave, …" — the same name
offered repeatedly, never resolving.
**Root cause:** MBTA returns **separate stop_ids per direction** at one corner, so a name
can map to several ids; our resolver listed those duplicates *and* re-resolved the
answer **by text**, which re-matched the duplicates → loop.
**Fix:** group stops by name; carry **all** stop_ids for a name (`stop_ids` tuple,
queried comma-joined with the direction filter); resolve a disambiguation answer by the
chosen option's **concrete ids**, never by re-fuzzy-matching text.
**Lesson:** real transit data is messier than the happy path. Resolving by stable ids
(not by re-matching free text) makes a loop structurally impossible.

### 9. Smarter matching — distinctive words beat fuzzy noise
**Symptom:** "Eutaw St" offered three stops, only one of which contained "Eutaw" — the
others matched only on the shared word "St."
**Fix:** prefer stops that contain the location's *distinctive* words (drop common
street words like St/Ave/Station). One container → it wins outright.
**Outcome:** "Eutaw St" now resolves directly; genuinely ambiguous inputs still
disambiguate.

### 10. Recovery UX — never trap the user
**Decision:** an unrecognized answer re-asks **with** a "Sorry, I didn't catch that"
nudge; and a self-contained new query ("actually, the next 116 from Maverick…") **escapes**
a pending clarification instead of being swallowed as an answer.
**Why:** the first hardening pass killed the loop but could still leave a user stuck on a
question. Conversations need an exit at every step.

---

## Phase 1.5 — Observability, and the latency win it unlocked

### 11. Measure before optimizing — add tracing (Arize Phoenix)
**Decision:** add an opt-in observability layer (Arize Phoenix via OpenInference /
OpenTelemetry) before touching anything for "latency." Off by default; async
`BatchSpanProcessor` export so it adds **no latency** to the request path; secrets
redacted; LangGraph + the LLM call auto-instrumented; one manual span at the MBTA HTTP
choke point.
**Why:** the "latency concern" was a *vibe*, not a number. You can't fix what you
haven't measured, and you shouldn't pay latency to measure latency.
**Lesson:** instrument once at the right seams (single LLM call-site, single HTTP client)
and the whole system becomes legible.

### 12. The data overturned the model choice (~7× faster)
**Measurement (first trace):** the router `gpt-5-mini` call was **~7,000 ms** vs an MBTA
call at **~168 ms** — the LLM was ~98% of every turn. Token counts showed why: **640
reasoning tokens** spent "thinking" about a trivial slot extraction.
**Decision:** switch the router to the **non-reasoning** `gpt-4.1-mini` (deployment
`gpt-4.1-mini-1234`), `temperature=0`, output capped at 512 tokens.
**Outcome:** router latency dropped to **~0.8–1.2 s** warm — roughly **6–9× faster** —
with identical slot quality.
**Lesson:** "best model" ≠ "fastest adequate." Reasoning models are overkill for trivial
structured extraction. The observability layer **paid for itself on its very first
trace**, and we now optimize from data, not hunches.

---

## TL;DR — lessons worth carrying forward

- **Measure before optimizing.** Tracing turned a "latency vibe" into "the LLM is 98% of
  the turn," then into a 6–9× win.
- **Right model for the job, not the biggest.** A non-reasoning model beat a reasoning
  one for slot extraction on both latency and (equal) quality.
- **Keep the LLM away from facts.** It extracts intent; templates produce times.
- **Resolve by stable ids, not by re-matching text** — that's what makes loops
  impossible and conversations recoverable.
- **Defend invariants over literal spec wording**, and flag the deviation.
- **Validate config where it's used**, so the fast path stays free of the LLM's deps.
