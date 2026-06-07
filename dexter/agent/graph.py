"""LangGraph wiring: router -> skill node -> format, with a checkpointer.

The checkpointer (``MemorySaver`` in Phase 1) keys conversation state by
``thread_id`` (the session id), which is what makes follow-ups and disambiguation
resolution work across turns.

Routing after the router:
- a `pending` disambiguation in state means this turn answers a clarifying
  question -> `clarify`;
- otherwise route by intent (predictions / alerts / facilities), else `fallback`.
"""

from __future__ import annotations

import functools

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from dexter.mbta.alerts import AlertsService
from dexter.mbta.facilities import FacilitiesService
from dexter.mbta.predictions import DeparturesService
from dexter.mbta.resolution import Resolver
from dexter.mbta.stations import StationCache

from .formatting import format_node
from .nodes import (
    alerts_node,
    clarify_node,
    facilities_node,
    fallback_node,
    predictions_node,
    router_node,
)
from .router import Router
from .state import AgentState

_INTENT_TO_NODE = {
    "predictions": "predictions",
    "alerts": "alerts",
    "facilities": "facilities",
}


def _is_fresh_query(state: AgentState) -> bool:
    """A self-contained new request (route + location) overrides a pending question."""
    return bool(state.get("route") and state.get("location"))


def _should_clarify(state: AgentState) -> bool:
    """True unless this turn is clearly a new request rather than an answer.

    A pending clarification consumes the turn by default (the user is answering it).
    It's escaped only when the turn is a complete new request (route + location) or
    switches to a different skill with a concrete route/stop — so the user isn't
    trapped, while short answers ("blue", "toward Maverick") still resolve the
    question even though the model flags them inconsistently as follow-ups.
    """
    if state.get("pending") is None:
        return False
    if _is_fresh_query(state):
        return False
    intent, pending_intent = state.get("intent"), state.get("pending_intent")
    switched_skill = bool(intent and pending_intent and intent != pending_intent)
    if switched_skill and (state.get("route") or state.get("location")):
        return False
    return True


def _route_after_router(state: AgentState) -> str:
    if _should_clarify(state):
        return "clarify"
    return _INTENT_TO_NODE.get(state.get("intent", ""), "fallback")


def build_graph(
    *,
    router: Router,
    resolver: Resolver,
    departures: DeparturesService,
    alerts: AlertsService | None = None,
    facilities: FacilitiesService | None = None,
    stations: StationCache | None = None,
    checkpointer=None,
):
    """Compile the Dexter agent graph. Pass ``checkpointer=False`` to disable it.

    The alerts/facilities skills default to services built on the resolver's shared
    MBTA client + route cache, so existing callers need only pass the core three.
    """
    alerts = alerts or AlertsService(resolver.client)
    facilities = facilities or FacilitiesService(resolver.client)
    stations = stations or StationCache(resolver.client)

    builder = StateGraph(AgentState)

    builder.add_node("router", functools.partial(router_node, router=router))
    builder.add_node(
        "predictions",
        functools.partial(predictions_node, resolver=resolver, departures=departures),
    )
    builder.add_node(
        "clarify",
        functools.partial(
            clarify_node,
            resolver=resolver,
            departures=departures,
            alerts=alerts,
            stations=stations,
            facilities=facilities,
        ),
    )
    builder.add_node(
        "alerts",
        functools.partial(alerts_node, resolver=resolver, alerts=alerts),
    )
    builder.add_node(
        "facilities",
        functools.partial(
            facilities_node, resolver=resolver, stations=stations, facilities=facilities
        ),
    )
    builder.add_node("fallback", fallback_node)
    builder.add_node("format", format_node)

    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router",
        _route_after_router,
        {
            "clarify": "clarify",
            "predictions": "predictions",
            "alerts": "alerts",
            "facilities": "facilities",
            "fallback": "fallback",
        },
    )
    for node in ("predictions", "clarify", "alerts", "facilities", "fallback"):
        builder.add_edge(node, "format")
    builder.add_edge("format", END)

    if checkpointer is None:
        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer or None)
