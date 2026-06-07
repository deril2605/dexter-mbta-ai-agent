"""LangGraph node functions (PRD §6.1).

Each node takes the conversation state and returns a partial update. Dependencies
(router, resolver, departures) are passed as keyword args; the graph (M5) binds
them with ``functools.partial`` at build time, and tests call the nodes directly.

Nodes set ``state["result"]`` to a structured outcome — they never produce
user-facing text. The formatter (M5) renders the reply.
"""

from __future__ import annotations

from datetime import datetime

from dexter.mbta.alerts import AlertsService
from dexter.mbta.client import MBTAError, MBTARateLimitError, MBTAUnavailableError
from dexter.mbta.facilities import FacilitiesService
from dexter.mbta.models import (
    AlertsResult,
    Disambiguation,
    DisambiguationKind,
    FacilitiesResult,
)
from dexter.mbta.predictions import DeparturesService
from dexter.mbta.resolution import Resolver, match_disambiguation_option
from dexter.mbta.routes import RouteCache
from dexter.mbta.stations import StationCache

from .router import Router
from .state import AgentState, Fallback, ServiceError


async def router_node(state: AgentState, *, router: Router) -> dict:
    """Classify intent + extract slots; reset per-turn output fields."""
    slots = await router.route(state["message"], history=state.get("history"))
    return {
        "intent": slots.intent,
        "route": slots.route,
        "location": slots.location,
        "direction_hint": slots.direction_hint,
        "follow_up": slots.follow_up,
        "offset": slots.offset,
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
    # Pure follow-up ("and the one after?") with nothing new -> reuse last target,
    # advancing the departure window by the requested offset (paging forward).
    if (
        state.get("follow_up")
        and state.get("last_target") is not None
        and not state.get("route")
        and not state.get("location")
        and not state.get("direction_hint")
    ):
        cumulative = (state.get("last_offset") or 0) + (state.get("offset") or 0)
        return await _fetch_for_target(state["last_target"], departures, now, cumulative)

    slots = {
        "route": state.get("route"),
        "location": state.get("location"),
        "direction_hint": state.get("direction_hint"),
    }
    # A fresh query starts at the soonest departure (offset is usually 0 here).
    return await _resolve_and_fetch(slots, resolver, departures, now, state.get("offset") or 0)


async def clarify_node(
    state: AgentState,
    *,
    resolver: Resolver,
    departures: DeparturesService,
    routes: RouteCache | None = None,
    alerts: AlertsService | None = None,
    stations: StationCache | None = None,
    facilities: FacilitiesService | None = None,
    now: datetime | None = None,
) -> dict:
    """Resolve the user's answer to a pending disambiguation.

    Dispatches by which skill asked the question (``pending_intent``). Predictions
    answers are resolved by the chosen option's **concrete ids** (never re-fuzzy-
    matched by text) so we can't loop; a route answer re-resolves from text. Alerts
    and facilities re-run their resolver with the answer merged into the slots.
    """
    pending: Disambiguation | None = state.get("pending")
    if pending is None:
        return {"result": Fallback(), "needs_input": False}

    base = state.get("pending_slots") or {}
    message = state.get("message", "")
    pending_intent = state.get("pending_intent")

    if pending_intent == "alerts":
        return await _resolve_alerts(
            state.get("route") or message,
            base.get("location"),
            routes=routes,
            resolver=resolver,
            alerts=alerts,
            now=now,
        )
    if pending_intent == "facilities":
        return await _resolve_facilities(
            state.get("location") or message,
            state.get("route") or message,
            routes=routes,
            stations=stations,
            facilities=facilities,
            now=now,
        )

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
                # A Green Line option carries its own serving-branch set; fall back to
                # the question's route for ordinary single-route stops.
                route_id=option.route_id or pending.route_id,
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


async def alerts_node(
    state: AgentState,
    *,
    routes: RouteCache,
    resolver: Resolver,
    alerts: AlertsService,
    now: datetime | None = None,
) -> dict:
    """Service alerts for a route, optionally narrowed to a stop."""
    return await _resolve_alerts(
        state.get("route"),
        state.get("location"),
        routes=routes,
        resolver=resolver,
        alerts=alerts,
        now=now,
    )


async def facilities_node(
    state: AgentState,
    *,
    routes: RouteCache,
    stations: StationCache,
    facilities: FacilitiesService,
    now: datetime | None = None,
) -> dict:
    """Elevator/escalator outages for a named station, else a route."""
    return await _resolve_facilities(
        state.get("location"),
        state.get("route"),
        routes=routes,
        stations=stations,
        facilities=facilities,
        now=now,
    )


async def fallback_node(state: AgentState) -> dict:
    return {"result": Fallback(), "needs_input": False}


# --- alerts / facilities resolution (shared by the node and clarify) ---------


def _skill_pending(disambiguation: Disambiguation, slots: dict, intent: str) -> dict:
    """A skill needs more input: carry the question + slots + which skill owns it."""
    return {
        "result": disambiguation,
        "pending": disambiguation,
        "pending_slots": slots,
        "pending_intent": intent,
        "needs_input": True,
    }


def _skill_result(result) -> dict:
    """A skill produced an answer: clear any pending clarification."""
    return {
        "result": result,
        "pending": None,
        "pending_slots": None,
        "pending_intent": None,
        "needs_input": False,
    }


async def _resolve_alerts(
    route_token: str | None,
    location: str | None,
    *,
    routes: RouteCache,
    resolver: Resolver,
    alerts: AlertsService,
    now: datetime | None,
) -> dict:
    slots = {"route": route_token, "location": location}
    if not route_token or not route_token.strip():
        return _skill_pending(Disambiguation(kind=DisambiguationKind.ROUTE), slots, "alerts")
    route = await routes.lookup(route_token)
    if route is None:
        question = Disambiguation(kind=DisambiguationKind.ROUTE, query=route_token)
        return _skill_pending(question, slots, "alerts")
    try:
        stop_ids = await resolver.stops_for(route.id, location)
        found = await alerts.get_alerts(route_id=route.id, stop_ids=stop_ids, now=now)
    except MBTARateLimitError:
        return {"result": ServiceError(kind="busy"), "needs_input": False}
    except (MBTAUnavailableError, MBTAError):
        return {"result": ServiceError(kind="unavailable"), "needs_input": False}
    return _skill_result(AlertsResult(scope_label=route.display_name, alerts=found))


async def _resolve_facilities(
    location: str | None,
    route_token: str | None,
    *,
    routes: RouteCache,
    stations: StationCache,
    facilities: FacilitiesService,
    now: datetime | None,
) -> dict:
    slots = {"location": location, "route": route_token}
    try:
        # Facilities are usually station-scoped, so try a named station first.
        if location and location.strip():
            station = await stations.lookup(location)
            if station is not None:
                outages = await facilities.get_outages(stop_ids=station.ids, now=now)
                return _skill_result(FacilitiesResult(scope_label=station.name, outages=outages))
        if route_token and route_token.strip():
            route = await routes.lookup(route_token)
            if route is not None:
                outages = await facilities.get_outages(route_id=route.id, now=now)
                return _skill_result(
                    FacilitiesResult(scope_label=route.display_name, outages=outages)
                )
    except MBTARateLimitError:
        return {"result": ServiceError(kind="busy"), "needs_input": False}
    except (MBTAUnavailableError, MBTAError):
        return {"result": ServiceError(kind="unavailable"), "needs_input": False}
    # Couldn't pin a station or a line — ask for one (and remember for next turn).
    return _skill_pending(
        Disambiguation(kind=DisambiguationKind.FACILITY_SCOPE), slots, "facilities"
    )


# --- shared helpers ---------------------------------------------------------


async def _resolve_and_fetch(
    slots: dict,
    resolver: Resolver,
    departures: DeparturesService,
    now: datetime | None,
    offset: int = 0,
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
                "pending_intent": "predictions",
                "needs_input": True,
            }
        result = await departures.get_departures(resolved, now=now, offset=offset)
    except MBTARateLimitError:
        return {"result": ServiceError(kind="busy"), "needs_input": False}
    except (MBTAUnavailableError, MBTAError):
        return {"result": ServiceError(kind="unavailable"), "needs_input": False}

    return {
        "result": result,
        "last_target": resolved,
        "last_offset": offset,
        "pending": None,
        "pending_slots": None,
        "pending_intent": None,
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
            "pending_intent": "predictions",
            "needs_input": True,
        }
    result = await departures.get_departures(resolved, now=now)
    return {
        "result": result,
        "last_target": resolved,
        "last_offset": 0,
        "pending": None,
        "pending_slots": None,
        "pending_intent": None,
        "needs_input": False,
    }


async def _fetch_for_target(
    target, departures: DeparturesService, now: datetime | None, offset: int = 0
) -> dict:
    try:
        result = await departures.get_departures(target, now=now, offset=offset)
    except MBTARateLimitError:
        return {"result": ServiceError(kind="busy"), "needs_input": False}
    except (MBTAUnavailableError, MBTAError):
        return {"result": ServiceError(kind="unavailable"), "needs_input": False}
    return {"result": result, "last_target": target, "last_offset": offset, "needs_input": False}
