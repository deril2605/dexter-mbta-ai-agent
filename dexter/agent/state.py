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
)


@dataclass(frozen=True, slots=True)
class SkillUnavailable:
    """A scaffolded skill the user asked for that isn't built yet."""

    skill: str  # "alerts" | "facilities"


@dataclass(frozen=True, slots=True)
class Fallback:
    """The message didn't map to any supported skill."""


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
    | Disambiguation
    | SkillUnavailable
    | Fallback
    | ServiceError
)


class AgentState(TypedDict, total=False):
    # --- input ---
    message: str
    history: list[dict]  # optional prior turns passed to the router

    # --- router-extracted slots (per turn) ---
    intent: str
    route: str | None
    location: str | None
    direction_hint: str | None
    follow_up: bool

    # --- cross-turn memory ---
    last_target: ResolvedTarget | None
    pending: Disambiguation | None
    pending_slots: dict | None  # the {route, location, direction_hint} that triggered `pending`
    pending_intent: str | None  # which skill owns `pending` (predictions/alerts/facilities)

    # --- output (per turn) ---
    result: Outcome | None
    reply: str
    needs_input: bool
    reclarify: bool  # set when an answer didn't match; prefixes a "didn't catch that"
