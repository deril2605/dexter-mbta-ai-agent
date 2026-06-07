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
from dexter.mbta.routes import RouteCache
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
    """A self-contained new request (route + location) overrides a pending question.

    Without this, a user mid-clarification who changes topic gets stuck, since
    every turn would be forced through `clarify`.
    """
    return bool(state.get("route") and state.get("location"))


def _route_after_router(state: AgentState) -> str:
    if state.get("pending") is not None and not _is_fresh_query(state):
        return "clarify"
    return _INTENT_TO_NODE.get(state.get("intent", ""), "fallback")


def build_graph(
    *,
    router: Router,
    resolver: Resolver,
    departures: DeparturesService,
    routes: RouteCache | None = None,
    alerts: AlertsService | None = None,
    facilities: FacilitiesService | None = None,
    stations: StationCache | None = None,
    checkpointer=None,
):
    """Compile the Dexter agent graph. Pass ``checkpointer=False`` to disable it.

    The alerts/facilities skills default to services built on the resolver's shared
    MBTA client + route cache, so existing callers need only pass the core three.
    """
    routes = routes or resolver.routes
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
            routes=routes,
            alerts=alerts,
            stations=stations,
            facilities=facilities,
        ),
    )
    builder.add_node(
        "alerts",
        functools.partial(alerts_node, routes=routes, resolver=resolver, alerts=alerts),
    )
    builder.add_node(
        "facilities",
        functools.partial(facilities_node, routes=routes, stations=stations, facilities=facilities),
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
