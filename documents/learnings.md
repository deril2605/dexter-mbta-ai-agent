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

## Phase 1.5 — Direction inference + alerts/facilities (2026-06-07)

### 13. Infer direction from two stops, instead of asking
**Problem:** "Blue Line from Airport to Bowdoin" worked (Bowdoin is a terminus), but
"Airport to Government Center" still asked "toward Wonderland or Bowdoin?" — even though
two named stops already pin the direction.
**Decision:** when a direction hint isn't a terminus/direction name, treat it as a
*destination stop* and infer direction from **stop order** — fetch
`/stops?filter[route]&filter[direction_id]` (MBTA returns it in travel order) and pick the
direction where origin precedes destination. Kept it in the LLM-free library, reusing the
existing `direction_hint` slot (no router change), and made it conservative: infer only
when exactly one direction qualifies, else fall back to asking — never guess wrong.
**Outcome:** live-verified the ordering assumption against the API (Airport idx 6 < Government
Center idx 10 toward Bowdoin; reversed the other way). 5 new regression tests.
**Lesson:** the API already encodes the answer (stop sequence) — prefer deriving facts from
its data over adding a turn of conversation.

### 14. One skill became three by reframing facilities as alerts
**Decision:** built the `alerts` skill (`/alerts` → typed `Alert`, severity/effect mapped to
plain language in the *formatter*, not the library), then realized **elevator/escalator
outages are just alerts with closure effects** — so `facilities` reuses the alert parser,
only adding the `filter[activity]=ALL` quirk (accessibility alerts are hidden by default).
**Trade-off flagged & decided with Deril:** facilities supports **station-by-name** ("elevator
at Park Street"), which has no route — so a new bounded `StationCache`
(`/stops?filter[location_type]=1`) fuzzy-matches *stations only*, a deliberate narrower
exception to "never fuzzy-match across all stops."
**Outcome:** alerts + facilities live, stubs removed; +13 tests (116 total), ruff clean.

### 15. Live testing tightened two things tests didn't catch
**Multi-turn clarify for the new skills:** alerts/facilities originally just re-asked when
scope was missing (no cross-turn memory). Generalized the `pending`/`clarify` machinery with a
`pending_intent` field so `clarify_node` dispatches the answer back to the skill that asked —
"any alerts?" → "the Blue Line" now resolves.
**Alert relevance:** a live session showed "is the Blue Line running?" leading with a generic
`NOTICE` ("predictions temporarily unavailable") that tied on severity with the real track-work
`DELAY`. **Decision:** drop purely-informational effects (`NOTICE`/`SUMMARY`) in the alerts
skill, so disruptions always lead and a line with only a feed-notice reads as clear (Green Line
→ "no current service alerts"). Live-verified.
**Lesson:** unit tests prove the wiring; **driving the real app surfaces relevance/UX problems
the mocks can't** — sort ties and "technically-correct-but-noisy" data only show up live.

### 16. Closed the follow-up gap, and added a router eval to measure accuracy
**Follow-up offset:** "and the one after that?" now returns *later* departures (PRD §10
scenario 2, previously a documented gap). Added an `offset` slot to the router and a cumulative
`last_offset` in state; `DeparturesService` pages the departure window forward (offset past the
end reads as "that's the last one").
**"Is it running?":** an alerts query with no disruptions now leads "The {line} is running
normally." instead of the flat "no alerts" line.
**Router eval harness:** a `pytest -m eval` suite scores the *real* router on ~30 labeled
utterances (intent exact-match, slots lenient), deselected by default so the normal loop stays
fast/offline; skips cleanly without Azure creds. First run: **100% intent, 100% slots** on
gpt-4.1-mini.
**Lesson:** a model swap is only safe to *keep* once you can measure accuracy, not just latency
— the eval is the accuracy counterpart to the Phoenix latency win, and it now covers the exact
phrasings we hand-tested (two-stop direction, "the one after that", "is it running").

### 17. The Green Line is a line, not a route — the stop picks the branch
**Bug (live):** "green line from Northeastern" asked "toward Boston College or Government
Center?" — wrong branch entirely. Cause: the Green Line is four routes (Green-B/C/D/E);
`lookup("green line")` fuzzy-collapsed to one arbitrary branch (Green-B), so a stop on another
branch (Northeastern is E-only) couldn't be found and mis-matched.
**Fix:** a generic "green line" token now resolves across all branches — match the stop over the
union, and the serving branch(es) decide. A branch-only stop (Northeastern → Green-E) resolves
cleanly; a **trunk** stop (Park Street, on all four) keeps every branch in the `route_id`
(comma-joined, which `/predictions filter[route]` accepts) and the chosen destination later
narrows back ("toward Government Center" → Green-B,Green-C). All in the LLM-free library; the
clarify path needed only a one-line tweak (a stop option carries its own branch set).
**Also surfaced + fixed:** `_match_stop` now lets an **exact** stop-name match win over a rival
that only shares a token ("Park Street" vs "Mission Park").
**Lesson:** model the domain's real shape. Blue/Orange/Red(ish) are single routes so they
"just worked"; the Green Line's branch structure is the actual data model, and resolution has
to mirror it. Driving the live app (not mocks) is what exposed it.

### 18. Made it conversational — and finished the Green Line
A live session exposed that "memory" wasn't what users expect.
- **Green Line in alerts/facilities:** "is the green line running today" returned "The B …" —
  the alerts skill still collapsed the line to one branch. Added `Resolver.resolve_scope`, so
  alerts/facilities span all branches (comma-joined `filter[route]`) and label "Green Line"
  (predictions already did this). Live: it now surfaces real C- and E-branch alerts.
- **Conversational memory (the real fix):** the LLM router was only ever shown the *latest*
  message — the `history` wiring existed but was never populated. `format_node` now records each
  turn into `state["history"]` (capped to ~3 exchanges, persisted by the checkpointer), so the
  router resolves "it", "wb blue line", and cross-skill follow-ups. Live-verified the exact
  reported thread.
- **Escape stale clarifications:** the graph routes to `clarify` only when the turn is actually
  *answering* the pending question (follow-up, same skill); a new/standalone/topic-switching
  question escapes instead of being swallowed as a stop-name answer.
- **Lesson:** "multi-turn" is two layers — a deterministic slot store (prevents hallucinated
  facts) *and* LLM conversation context (makes it feel like a chat). We'd built only the first;
  users expect both.
- **Known follow-up:** LangGraph warns about msgpack-serializing the result dataclasses
  (`Alert`/`AlertsResult`) held in checkpointed state — benign now, but register the types or
  drop `result` from the checkpoint before it's enforced.

### 19. Feeding history isn't enough — tell the model to use it (and don't gate on flaky flags)
Right after wiring conversation history in, "is the Blue Line running?" → "when's the next train
to Government Center" still asked "which route?". Two follow-on fixes:
- **Prompt must instruct context use.** The router was *given* history but its prompt said to
  extract the route "exactly as the user said it" and "leave a slot out when absent" — so it
  refused to carry an implied route. Added an explicit instruction to carry established slots
  (above all the route) from earlier turns. Live: it now infers "Blue Line", and treats
  "to Government Center" as a destination — then a boarding stop ("from airport") triggers the
  two-stop direction inference end to end.
- **Don't gate control flow on an unreliable LLM flag.** The clarify-escape gate first required
  `follow_up=True`, but the eval showed the model marks short answers ("the 116", "toward
  Maverick", "blue") as `follow_up=False` inconsistently — which would make real disambiguation
  answers *escape* and lose context. Reworked the gate to clarify by default and escape only on
  a complete new request or a skill switch — deterministic signals, not the model's mood.
- **Eval earns its keep:** added history-bearing cases; they're what caught the `follow_up`
  flakiness and proved context inference. 100% intent / 100% slots across 33 cases.
- **Carry-over needs a scope rule.** "carry established slots" was too broad: after a Blue Line /
  Airport turn, "what about the 116 to Maverick" sometimes inherited *Airport* as the 116's
  boarding stop (nonsensical — the 116 doesn't serve it). A boarding stop is route-specific, so
  the prompt now carries the *route* but never reuses a *stop* across a route change. Added a
  `forbid` mechanism to the eval (slots that must stay empty) + a case for this; two live runs
  at 100% confirmed it's stable, not just lucky.
- **Name the boarding stop in the answer.** The prediction/schedule reply now reads "The next 116
  **from [stop]** toward Maverick is in …" (shared `_target_descriptor`). Beyond being clearer,
  it makes a wrong stop *visible* — the first live test surfaced "Bennington Street" resolving to
  "Meridian St at Lexington St" on the 116, a pre-existing match bug that had been invisible.
  Lesson: show the resolved entity, not just the answer — transparency turns silent
  mis-resolutions into catchable ones.

### 20. Trustworthy stop resolution: confident-or-ask + cross-route awareness
Live, "green line from south station" answered for **South Street** — far away, plain wrong.
Three compounding causes and fixes (the matcher is `rapidfuzz`, not regex; the issue was loose
*acceptance*):
- **Tokenization:** "station" was a dropped stopword, so "South Station" → "south" → "South
  Street". Keep place-name words (station/square/circle) significant.
- **Confidence gate + conflict guard:** a single weak fuzzy/containment match won silently. Now a
  fuzzy match wins only when strong AND not a *different place* — `_token_conflict` rejects a
  candidate that has a distinctive word the user didn't say while lacking one they did ("South
  Station" vs "South Street"), but allows a pure shortening ("Harvard" for "Harvard Square").
  Weak matches become candidates/not-found, never silent winners.
- **Cross-route awareness:** on not-found, check the global `StationCache`; if the location is a
  real station the route doesn't serve (authoritatively via `/routes?filter[stop]`), return
  `StopNotOnRoute` → "South Station isn't on the Green Line — it's served by the Red Line, Silver
  Line, and Commuter Rail." (long bus lists collapse to "local buses").
- **Lesson:** fuzzy matching needs a confidence floor + a conflict check + a "wrong domain"
  fallback, or it confidently returns nonsense. Together with showing the boarding stop in replies
  (#18-era), the assistant now *asks when unsure and is transparent when sure* — the core of "never
  give wrong info."

---

## Phase 1.5 — Beta web client + Azure Container Apps deployment (2026-06-07)

Goal: let a friend beta-test over a shareable link — no repo clone, no scripts — while
keeping the option to tear it all out trivially.

- **The web UI is the CLI's twin, not a new layer.** Because `cli/repl.py` was already a
  dumb client that only POSTs `/chat`, a browser client adds *zero* code to brain/agent/mbta.
  A single static `web/index.html` (terminal-styled, client-side typewriter, no framework,
  no `xterm.js`) talks to the same endpoint. Honors "no logic in the client" for free.
- **Decoupling = folders + a default-off flag, not a separate repo.** Pushed back on a
  second repository: it buys no isolation but adds `/chat` contract drift and two pipelines.
  Instead `web/` and `deploy/` are sibling folders; `dexter/` imports neither. Removal is
  `rm -rf web/ deploy/` + `DEXTER_SERVE_WEB=false` → exactly Phase 1 again.
- **Gate before the LLM.** The passcode check (`X-Dexter-Passcode`) sits at the top of
  `/chat`, before any graph/Azure call, so a leaked link can't burn quota. The gate only
  *appears* in the UI on a real 401 — ungated/local runs never prompt.
- **Lazy-config discipline held.** Web/gate state lives on `app.state`, defaulted in
  `create_app` and set from `Settings` only in `_build_runtime` (lifespan). Importing the
  module still triggers no validation, so the 150-test suite stays offline and green.
- **Tracing is server-side — the friend's browser emits nothing.** Spans come from the
  brain, so remote testers' sessions land wherever the brain points. Local Phoenix is
  in-memory/ephemeral and unreachable from a cloud brain, so beta history goes to **Phoenix
  Cloud** via OTLP + an `api_key` header. Split the `tracing` extra into a slim client
  (`arize-phoenix-otel` only, shipped in the image) vs `tracing-local` (full server).
- **Container gotcha:** `pip install .` relocates `dexter/` into site-packages, so a
  source-relative path to `web/` breaks in the image. Resolve the static file CWD-first,
  source-relative as fallback — works for the container (WORKDIR `/app`), editable installs,
  and tests alike.
- **ACA setting that matters:** `--min-replicas 1`. Scale-to-zero would put a cold start +
  route-cache re-warm in front of the already-slow router LLM on the first message. Budget's
  not the constraint; a warm replica is.

---

## Phase 1.5 — Conversational smalltalk, the right way (2026-06-08)

Beta feedback: a greeting ("hi") got a canned line ("Anytime — just ask…"), which read
like a robot. First attempt template-matched smalltalk to one fixed string — still wrong,
because greeting / thanks / sign-off all collapsed to the same sentence.

- **Let the model write social replies; keep facts templated.** Added a `smalltalk`
  intent and a dedicated `Router.smalltalk()` LLM call that writes one short, natural,
  contextual sentence (greeting greets back, thanks gets "you're welcome"). The reply is
  carried as a `SmallTalk(text=...)` outcome and rendered verbatim.
- **This is not a violation of "the LLM never produces user text."** That invariant exists
  to prevent **fabricated times/transit facts**. The smalltalk prompt forbids any transit
  specifics, and the node only runs for the `smalltalk` intent — real departures still come
  from templates. A misclassified transit question fails safe (a friendly "what route?",
  never an invented time).
- **Cost is a second LLM call, but only on social turns** (rare), so latency on the hot
  predictions path is unchanged. Used temperature 0.6 here for warmth/variety vs the
  router's 0.0 deterministic extraction.
- **Lesson:** "LLM-free for facts" ≠ "LLM-free for everything." Draw the safety boundary
  around the thing that can actually be wrong (times), and let the model be human elsewhere.

---

## Phase 1.5 — GitHub Actions CD for a public repo, the careful way (2026-06-08)

Goal: stop relying on a laptop-local Docker build for production deploys while
keeping the shared Azure setup safe enough for a public repository.

- **Split CI from CD.** Pull requests now run validation only; production deploys
  happen only on `push` to `main` or manual dispatch. That keeps untrusted PRs
  away from Azure access entirely.
- **OIDC beat secrets.** For a public repo, storing a long-lived Azure client
  secret in GitHub would be the easy path and the wrong one. Switched to GitHub
  OIDC + a federated credential on an Entra app, so GitHub only presents a
  signed identity assertion and Azure issues the token.
- **Narrower than `deploy.py` on purpose.** The local deploy script can create
  resources, enable ACR admin access, and manage shared infra. The GitHub
  workflow should not. It only builds, pushes, updates the existing Container
  App, and smoke-tests `/health`.
- **Environment protection is part of the trust model.** Using the GitHub
  `production` environment in both GitHub and the Azure federated credential
  ties approval and authentication together cleanly.
- **A visible deploy signal matters.** The app already exposed a deploy-time
  stamp through the UI header; that became the perfect sanity check that the CD
  path really updated production. Seeing `last redeployed` change proved the
  merge-to-ACA path end to end without needing deeper observability first.
- **Public repo lesson:** the main risk is not GitHub Actions billing. Standard
  runners are effectively free here; the real concerns are credential shape,
  blast radius, and keeping the deployment identity as narrow as possible.

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
