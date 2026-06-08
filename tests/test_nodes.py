"""Milestone 4 — graph nodes (MBTA respx-mocked, no LLM).

Node mechanics: predictions resolution/fetch, error mapping, follow-up reuse,
disambiguation produce/resolve, and the stub/fallback branches. Full multi-turn
through the graph + checkpointer is M5.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from dexter.agent.nodes import (
    alerts_node,
    clarify_node,
    facilities_node,
    fallback_node,
    predictions_node,
    smalltalk_node,
)
from dexter.agent.state import Fallback, ServiceError, SmallTalk
from dexter.mbta.alerts import AlertsService
from dexter.mbta.client import MBTAClient
from dexter.mbta.facilities import FacilitiesService
from dexter.mbta.models import (
    AlertsResult,
    Disambiguation,
    DisambiguationKind,
    DisambiguationOption,
    FacilitiesResult,
    NoServiceResult,
    PredictionResult,
)
from dexter.mbta.predictions import DeparturesService
from dexter.mbta.resolution import Resolver
from dexter.mbta.routes import RouteCache
from dexter.mbta.stations import StationCache

from .conftest import MBTA_BASE_URL, NOW, ROUTES_PAYLOAD, TARGET


def stops_payload(*stops: tuple[str, str]) -> dict:
    return {
        "data": [{"type": "stop", "id": sid, "attributes": {"name": name}} for sid, name in stops]
    }


def predictions_payload(*minutes: float) -> dict:
    return {
        "data": [
            {
                "type": "prediction",
                "id": str(i),
                "attributes": {
                    "departure_time": (NOW + timedelta(minutes=m)).isoformat(),
                    "arrival_time": None,
                },
            }
            for i, m in enumerate(minutes)
        ]
    }


@pytest.fixture
def deps(respx_mock):
    """A live-wired Resolver + DeparturesService over a respx-mocked MBTA API."""
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json=ROUTES_PAYLOAD)
    )
    client = MBTAClient(base_url=MBTA_BASE_URL)
    resolver = Resolver(client, RouteCache(client))
    departures = DeparturesService(client)
    return SimpleDeps(respx_mock, client, resolver, departures)


class SimpleDeps:
    def __init__(self, respx_mock, client, resolver, departures):
        self.respx = respx_mock
        self.client = client
        self.resolver = resolver
        self.departures = departures


# --- predictions_node -------------------------------------------------------


async def test_predictions_node_resolves_and_returns_predictions(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=predictions_payload(4, 12))
    )
    state = {"route": "116", "location": "Maverick", "direction_hint": "Wonderland"}
    update = await predictions_node(
        state, resolver=deps.resolver, departures=deps.departures, now=NOW
    )

    assert isinstance(update["result"], PredictionResult)
    assert update["result"].minutes_away == (4, 12)
    assert update["last_target"].route_id == "116"
    assert update["needs_input"] is False
    await deps.client.aclose()


async def test_predictions_node_follow_up_offset_pages_forward(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=predictions_payload(4, 12, 19, 27))
    )
    state = {"follow_up": True, "last_target": TARGET, "last_offset": 0, "offset": 1}
    update = await predictions_node(
        state, resolver=deps.resolver, departures=deps.departures, now=NOW
    )
    assert update["result"].minutes_away == (12, 19, 27)
    assert update["last_offset"] == 1
    await deps.client.aclose()


async def test_predictions_node_offset_accumulates_across_turns(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=predictions_payload(4, 12, 19, 27))
    )
    # Already paged once (last_offset=1); another "one after that" (offset=1) -> 2.
    state = {"follow_up": True, "last_target": TARGET, "last_offset": 1, "offset": 1}
    update = await predictions_node(
        state, resolver=deps.resolver, departures=deps.departures, now=NOW
    )
    assert update["result"].minutes_away == (19, 27)
    assert update["last_offset"] == 2
    await deps.client.aclose()


async def test_predictions_node_missing_direction_sets_pending(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    state = {"route": "116", "location": "Maverick", "direction_hint": None}
    update = await predictions_node(
        state, resolver=deps.resolver, departures=deps.departures, now=NOW
    )

    assert isinstance(update["result"], Disambiguation)
    assert update["result"].kind is DisambiguationKind.DIRECTION
    assert update["needs_input"] is True
    # The slots that triggered the question are stored for the next turn.
    assert update["pending_slots"]["location"] == "Maverick"
    await deps.client.aclose()


async def test_predictions_node_maps_rate_limit_to_service_error(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(return_value=httpx.Response(429))
    state = {"route": "116", "location": "Maverick", "direction_hint": "Wonderland"}
    update = await predictions_node(
        state, resolver=deps.resolver, departures=deps.departures, now=NOW
    )

    assert update["result"] == ServiceError(kind="busy")
    await deps.client.aclose()


async def test_predictions_node_follow_up_reuses_last_target(deps):
    # No new slots, follow_up=True, last_target present -> reuse it, no resolution.
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=predictions_payload(6, 14))
    )
    state = {"follow_up": True, "last_target": TARGET}
    update = await predictions_node(
        state, resolver=deps.resolver, departures=deps.departures, now=NOW
    )

    assert isinstance(update["result"], PredictionResult)
    assert update["result"].target is TARGET
    assert update["result"].minutes_away == (6, 14)
    await deps.client.aclose()


# --- clarify_node -----------------------------------------------------------


async def test_clarify_node_resolves_direction_answer(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=predictions_payload(5))
    )
    pending = Disambiguation(
        kind=DisambiguationKind.DIRECTION,
        options=(
            DisambiguationOption(label="Wonderland", direction_id=0),
            DisambiguationOption(label="Maverick", direction_id=1),
        ),
        route_id="116",
        stop_ids=("70",),
        stop_name="Maverick Station",
    )
    state = {
        "message": "toward Wonderland",
        "pending": pending,
        "pending_slots": {"route": "116", "location": "Maverick", "direction_hint": None},
    }
    update = await clarify_node(state, resolver=deps.resolver, departures=deps.departures, now=NOW)

    assert isinstance(update["result"], PredictionResult)
    assert update["result"].target.direction_destination == "Wonderland"
    assert update["result"].target.direction_id == 0
    assert update["pending"] is None
    await deps.client.aclose()


async def test_clarify_stop_answer_uses_option_ids_no_loop(deps):
    # Answering a STOP question must resolve by the option's concrete ids and move
    # ON (to a direction question here) — never re-ask the same stop (the loop bug).
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=predictions_payload(5))
    )
    pending = Disambiguation(
        kind=DisambiguationKind.STOP,
        options=(
            DisambiguationOption(label="S Huntington Ave @ Huntington Ave", stop_ids=("11", "22")),
            DisambiguationOption(label="Maverick Station", stop_ids=("70",)),
        ),
        route_id="116",
    )
    state = {
        "message": "S Huntington Ave at Huntington Ave",
        "pending": pending,
        "pending_slots": {"route": "116", "location": "S Huntington", "direction_hint": None},
    }
    update = await clarify_node(state, resolver=deps.resolver, departures=deps.departures, now=NOW)

    # 116 has two directions, so the next step is a DIRECTION question carrying the
    # chosen stop's ids — NOT another identical STOP question.
    assert isinstance(update["result"], Disambiguation)
    assert update["result"].kind is DisambiguationKind.DIRECTION
    assert set(update["result"].stop_ids) == {"11", "22"}
    await deps.client.aclose()


async def test_clarify_unmatched_answer_reasks_with_nudge(deps):
    pending = Disambiguation(
        kind=DisambiguationKind.DIRECTION,
        options=(
            DisambiguationOption(label="Wonderland", direction_id=0),
            DisambiguationOption(label="Maverick", direction_id=1),
        ),
        route_id="116",
        stop_ids=("70",),
        stop_name="Maverick Station",
    )
    state = {"message": "banana", "pending": pending, "pending_slots": {}}
    update = await clarify_node(state, resolver=deps.resolver, departures=deps.departures, now=NOW)

    assert update["result"] is pending  # same question re-asked
    assert update["needs_input"] is True
    assert update["reclarify"] is True  # formatter will add "Sorry, I didn't catch that."
    await deps.client.aclose()


async def test_clarify_node_without_pending_falls_back(deps):
    update = await clarify_node(
        {"message": "huh"}, resolver=deps.resolver, departures=deps.departures, now=NOW
    )
    assert update["result"] == Fallback()
    await deps.client.aclose()


# --- alerts / facilities branches ------------------------------------------


def alerts_payload(*alerts: tuple[str, int, str]) -> dict:
    """Build an /alerts payload from (effect, severity, header) tuples."""
    return {
        "data": [
            {
                "type": "alert",
                "id": str(i),
                "attributes": {"effect": e, "severity": s, "header": h, "short_header": h},
            }
            for i, (e, s, h) in enumerate(alerts)
        ]
    }


async def test_alerts_node_returns_active_alerts(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200, json=alerts_payload(("DELAY", 5, "Blue Line delays of about 10 minutes."))
        )
    )
    update = await alerts_node(
        {"route": "Blue Line"},
        resolver=deps.resolver,
        alerts=AlertsService(deps.client),
        now=NOW,
    )
    assert isinstance(update["result"], AlertsResult)
    assert update["result"].scope_label == "Blue Line"
    assert len(update["result"].alerts) == 1
    assert update["needs_input"] is False
    await deps.client.aclose()


async def test_alerts_node_missing_route_asks(deps):
    update = await alerts_node({}, resolver=deps.resolver, alerts=AlertsService(deps.client))
    assert isinstance(update["result"], Disambiguation)
    assert update["result"].kind is DisambiguationKind.ROUTE
    assert update["needs_input"] is True
    await deps.client.aclose()


async def test_alerts_node_maps_error_to_service_error(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/alerts").mock(return_value=httpx.Response(503))
    update = await alerts_node(
        {"route": "Blue Line"},
        resolver=deps.resolver,
        alerts=AlertsService(deps.client),
        now=NOW,
    )
    assert update["result"] == ServiceError(kind="unavailable")
    await deps.client.aclose()


async def test_alerts_node_green_line_spans_all_branches(deps):
    # "green line" must cover every branch and read "Green Line", not collapse to one.
    route = deps.respx.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(200, json=alerts_payload())
    )
    update = await alerts_node(
        {"route": "green line"},
        resolver=deps.resolver,
        alerts=AlertsService(deps.client),
        now=NOW,
    )
    assert isinstance(update["result"], AlertsResult)
    assert update["result"].scope_label == "Green Line"
    assert route.calls.last.request.url.params["filter[route]"] == "Green-B,Green-C,Green-D,Green-E"
    await deps.client.aclose()


async def test_facilities_node_returns_outages_for_station(deps):
    deps.respx.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("place-pktrm", "Park Street")))
    )
    deps.respx.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200,
            json=alerts_payload(("ELEVATOR_CLOSURE", 7, "Park Street elevator unavailable.")),
        )
    )
    update = await facilities_node(
        {"location": "Park Street"},
        resolver=deps.resolver,
        stations=StationCache(deps.client),
        facilities=FacilitiesService(deps.client),
        now=NOW,
    )
    assert isinstance(update["result"], FacilitiesResult)
    assert update["result"].scope_label == "Park Street"
    assert len(update["result"].outages) == 1
    await deps.client.aclose()


async def test_facilities_node_missing_scope_asks(deps):
    update = await facilities_node(
        {},
        resolver=deps.resolver,
        stations=StationCache(deps.client),
        facilities=FacilitiesService(deps.client),
    )
    assert isinstance(update["result"], Disambiguation)
    assert update["needs_input"] is True
    await deps.client.aclose()


async def test_fallback_node():
    update = await fallback_node({"message": "what's the weather?"})
    assert update["result"] == Fallback()


async def test_smalltalk_node_uses_model_reply():
    class FakeRouter:
        def __init__(self):
            self.seen = None

        async def smalltalk(self, message, *, history=None):
            self.seen = (message, history)
            return "Hi there! Where are you headed?"

    router = FakeRouter()
    update = await smalltalk_node({"message": "hello", "history": []}, router=router)

    assert update["result"] == SmallTalk(text="Hi there! Where are you headed?")
    assert router.seen == ("hello", [])


async def test_smalltalk_node_survives_model_error():
    class BoomRouter:
        async def smalltalk(self, *_args, **_kwargs):
            raise RuntimeError("llm down")

    update = await smalltalk_node({"message": "hi"}, router=BoomRouter())
    assert isinstance(update["result"], SmallTalk)
    assert update["result"].text  # falls back to a default greeting, never blank


async def test_no_service_outcome_propagates(deps):
    # Predictions empty + schedule empty -> NoServiceResult flows through the node.
    deps.respx.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    deps.respx.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    deps.respx.get(f"{MBTA_BASE_URL}/schedules").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    state = {"route": "116", "location": "Maverick", "direction_hint": "Wonderland"}
    update = await predictions_node(
        state, resolver=deps.resolver, departures=deps.departures, now=NOW
    )
    assert isinstance(update["result"], NoServiceResult)
    await deps.client.aclose()
