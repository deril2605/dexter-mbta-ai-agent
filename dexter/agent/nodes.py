"""LangGraph node functions (PRD §6.1).

Each node takes the conversation state and returns a partial update. Dependencies
(router, resolver, departures) are passed as keyword args; the graph (M5) binds
them with ``functools.partial`` at build time, and tests call the nodes directly.

Nodes set ``state["result"]`` to a structured outcome — they never produce
user-facing text. The formatter (M5) renders the reply.
"""

from __future__ import annotations

from datetime import datetime

from dexter.mbta.client import MBTAError, MBTARateLimitError, MBTAUnavailableError
from dexter.mbta.models import Disambiguation, DisambiguationKind
from dexter.mbta.predictions import DeparturesService
from dexter.mbta.resolution import Resolver, match_disambiguation_option

from .router import Router
from .state import AgentState, Fallback, ServiceError, SkillUnavailable


async def router_node(state: AgentState, *, router: Router) -> dict:
    """Classify intent + extract slots; reset per-turn output fields."""
    slots = await router.route(state["message"], history=state.get("history"))
    return {
        "intent": slots.intent,
        "route": slots.route,
        "location": slots.location,
        "direction_hint": slots.direction_hint,
        "follow_up": slots.follow_up,
        "result": None,
        "reply": "",
        "needs_input": False,
        "reclarify": False,
    }


async def predictions_node(
    state: AgentState,
    *,
    resolver: Resolver,
    departures: DeparturesService,
    now: datetime | None = None,
) -> dict:
    """Resolve the slots and fetch departures (with schedule fallback)."""
    # Pure follow-up ("and the one after?") with nothing new -> reuse last target.
    if (
        state.get("follow_up")
        and state.get("last_target") is not None
        and not state.get("route")
        and not state.get("location")
        and not state.get("direction_hint")
    ):
        return await _fetch_for_target(state["last_target"], departures, now)

    slots = {
        "route": state.get("route"),
        "location": state.get("location"),
        "direction_hint": state.get("direction_hint"),
    }
    return await _resolve_and_fetch(slots, resolver, departures, now)


async def clarify_node(
    state: AgentState,
    *,
    resolver: Resolver,
    departures: DeparturesService,
    now: datetime | None = None,
) -> dict:
    """Resolve the user's answer to a pending disambiguation.

    Stop and direction answers are resolved by the chosen option's **concrete
    ids** (never re-fuzzy-matched by text) so we can't loop. A route answer, or a
    bare "which stop?" reply with no options, re-resolves from text.
    """
    pending: Disambiguation | None = state.get("pending")
    if pending is None:
        return {"result": Fallback(), "needs_input": False}

    base = state.get("pending_slots") or {}
    message = state.get("message", "")
    option = match_disambiguation_option(pending, message)

    try:
        # `==` (not `is`): a StrEnum survives a checkpointer round-trip as its value.
        if pending.kind == DisambiguationKind.DIRECTION:
            if option is None:  # couldn't understand the answer — ask again, with a nudge
                return {
                    "result": pending,
                    "pending": pending,
                    "needs_input": True,
                    "reclarify": True,
                }
            resolved = await resolver.resolve_with_ids(
                route_id=pending.route_id,
                stop_ids=pending.stop_ids,
                stop_name=pending.stop_name or "",
                direction_hint=option.label,
            )
            return await _finish(resolved, base, departures, now)

        if pending.kind == DisambiguationKind.STOP and pending.options:
            if option is None:  # answer didn't match any offered stop — ask again
                return {"result": pending, "pending": pending, "needs_input": True}
            resolved = await resolver.resolve_with_ids(
                route_id=pending.route_id,
                stop_ids=option.stop_ids,
                stop_name=option.label,
                direction_hint=base.get("direction_hint"),
            )
            return await _finish(resolved, base, departures, now)
    except MBTARateLimitError:
        return {"result": ServiceError(kind="busy"), "needs_input": False}
    except (MBTAUnavailableError, MBTAError):
        return {"result": ServiceError(kind="unavailable"), "needs_input": False}

    # STOP with no options ("which stop?") -> the message is the location.
    # ROUTE -> the message (or extracted route) is the route. Re-resolve from text.
    if pending.kind == DisambiguationKind.ROUTE:
        slots = {
            "route": state.get("route") or message,
            "location": base.get("location"),
            "direction_hint": base.get("direction_hint"),
        }
    else:  # STOP without options
        slots = {
            "route": base.get("route"),
            "location": message or base.get("location"),
            "direction_hint": base.get("direction_hint"),
        }
    return await _resolve_and_fetch(slots, resolver, departures, now)


async def alerts_node(state: AgentState) -> dict:
    return {"result": SkillUnavailable(skill="alerts"), "needs_input": False}


async def facilities_node(state: AgentState) -> dict:
    return {"result": SkillUnavailable(skill="facilities"), "needs_input": False}


async def fallback_node(state: AgentState) -> dict:
    return {"result": Fallback(), "needs_input": False}


# --- shared helpers ---------------------------------------------------------


async def _resolve_and_fetch(
    slots: dict, resolver: Resolver, departures: DeparturesService, now: datetime | None
) -> dict:
    try:
        resolved = await resolver.resolve(
            slots.get("route"), slots.get("location"), slots.get("direction_hint")
        )
        if isinstance(resolved, Disambiguation):
            return {
                "result": resolved,
                "pending": resolved,
                "pending_slots": slots,
                "needs_input": True,
            }
        result = await departures.get_departures(resolved, now=now)
    except MBTARateLimitError:
        return {"result": ServiceError(kind="busy"), "needs_input": False}
    except (MBTAUnavailableError, MBTAError):
        return {"result": ServiceError(kind="unavailable"), "needs_input": False}

    return {
        "result": result,
        "last_target": resolved,
        "pending": None,
        "pending_slots": None,
        "needs_input": False,
    }


async def _finish(
    resolved, base: dict, departures: DeparturesService, now: datetime | None
) -> dict:
    """Turn a completed resolution into departures, or carry the next question."""
    if isinstance(resolved, Disambiguation):
        return {
            "result": resolved,
            "pending": resolved,
            "pending_slots": base,
            "needs_input": True,
        }
    result = await departures.get_departures(resolved, now=now)
    return {
        "result": result,
        "last_target": resolved,
        "pending": None,
        "pending_slots": None,
        "needs_input": False,
    }


async def _fetch_for_target(target, departures: DeparturesService, now: datetime | None) -> dict:
    try:
        result = await departures.get_departures(target, now=now)
    except MBTARateLimitError:
        return {"result": ServiceError(kind="busy"), "needs_input": False}
    except (MBTAUnavailableError, MBTAError):
        return {"result": ServiceError(kind="unavailable"), "needs_input": False}
    return {"result": result, "last_target": target, "needs_input": False}
