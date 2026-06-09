"""Conversation state and agent-level outcome types.

The graph's state is checkpointed per ``session_id`` (MemorySaver, wired in M5).
Most fields are per-turn (overwritten each message); ``last_target``, ``pending``
and ``pending_slots`` are the cross-turn memory that powers follow-ups and
disambiguation resolution.

Outcome types: the MBTA library returns ``PredictionResult`` / ``ScheduleResult``
/ ``NoServiceResult`` / ``Disambiguation``; the agent adds a few non-MBTA outcomes
(stub skills, fallback, transient errors). The formatter (M5) renders all of them
to speakable text — nodes never produce user-facing strings themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from dexter.mbta.models import (
    AlertsResult,
    Disambiguation,
    FacilitiesResult,
    NoServiceResult,
    PredictionResult,
    ResolvedTarget,
    ScheduleResult,
    StopNotOnRoute,
)


@dataclass(frozen=True, slots=True)
class SkillUnavailable:
    """A scaffolded skill the user asked for that isn't built yet."""

    skill: str  # "alerts" | "facilities"


@dataclass(frozen=True, slots=True)
class SmallTalk:
    """A conversational, non-transit reply written by the model (greeting/thanks/sign-off).

    ``text`` is the LLM's reply, rendered as-is. The model is constrained to never
    state transit facts here, so no times can leak — those stay templated.
    """

    text: str


@dataclass(frozen=True, slots=True)
class SavedCommuteConfirmation:
    """Confirms a commute the rider just saved (so they can verify what stuck)."""

    name: str
    route_name: str
    stop_name: str
    direction_destination: str
    walk_minutes: int


@dataclass(frozen=True, slots=True)
class LeaveNow:
    """A "should I leave now?" answer for a saved commute.

    ``departures`` is the underlying real-time/schedule/no-service result; the
    formatter subtracts ``walk_minutes`` to phrase "leave in X" — the LLM never
    computes the time.
    """

    name: str
    walk_minutes: int
    departures: PredictionResult | ScheduleResult | NoServiceResult


@dataclass(frozen=True, slots=True)
class NoSavedCommute:
    """Leave-now/lookup asked for a commute the rider hasn't saved."""

    name: str | None = None  # the name they asked for, if any


@dataclass(frozen=True, slots=True)
class SaveNeedsTrip:
    """A save request we couldn't pin to a concrete trip — ask for it first."""


@dataclass(frozen=True, slots=True)
class Fallback:
    """A non-transit request that isn't social chit-chat — nudge back to Dexter's job."""


@dataclass(frozen=True, slots=True)
class ServiceError:
    """A transient MBTA feed problem (PRD §11)."""

    kind: str  # "busy" (429) | "unavailable" (timeout/5xx)


# Everything format_node can turn into a reply.
Outcome = (
    PredictionResult
    | ScheduleResult
    | NoServiceResult
    | AlertsResult
    | FacilitiesResult
    | StopNotOnRoute
    | Disambiguation
    | SavedCommuteConfirmation
    | LeaveNow
    | NoSavedCommute
    | SaveNeedsTrip
    | SkillUnavailable
    | SmallTalk
    | Fallback
    | ServiceError
)


class AgentState(TypedDict, total=False):
    # --- input ---
    message: str
    history: list[dict]  # optional prior turns passed to the router
    user_id: str | None  # opaque per-rider token (saved commutes); None = anonymous

    # --- router-extracted slots (per turn) ---
    intent: str
    route: str | None
    location: str | None
    direction_hint: str | None
    follow_up: bool
    offset: int  # how far a "later departures" follow-up advances ("the one after that")
    commute_name: str | None  # name for save/leave-now ("morning", "work")
    walk_minutes: int | None  # walk time to the stop, for save_commute

    # --- cross-turn memory ---
    last_target: ResolvedTarget | None
    last_offset: int  # cumulative departure offset, so "the one after that" keeps paging
    pending: Disambiguation | None
    pending_slots: dict | None  # the {route, location, direction_hint} that triggered `pending`
    pending_intent: str | None  # which skill owns `pending` (predictions/alerts/facilities)

    # --- output (per turn) ---
    result: Outcome | None
    reply: str
    needs_input: bool
    reclarify: bool  # set when an answer didn't match; prefixes a "didn't catch that"
