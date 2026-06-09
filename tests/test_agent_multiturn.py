"""Milestone 5 — end-to-end graph: multi-turn, disambiguation, routing.

The LLM is faked (FakeRouter); MBTA HTTP is respx-mocked. Exercises the compiled
graph with a MemorySaver checkpointer across turns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from dexter.agent.graph import build_graph
from dexter.agent.router import RouterSlots
from dexter.mbta.client import MBTAClient
from dexter.mbta.models import Disambiguation, PredictionResult
from dexter.mbta.predictions import DeparturesService
from dexter.mbta.resolution import Resolver
from dexter.mbta.routes import RouteCache
from dexter.profiles import CommuteStore

from .conftest import MBTA_BASE_URL, ROUTES_PAYLOAD


class FakeRouter:
    """Returns canned slots per message (the LLM stand-in)."""

    def __init__(self, mapping: dict[str, RouterSlots]):
        self._mapping = mapping

    async def route(self, message: str, *, history=None) -> RouterSlots:
        return self._mapping[message]


def stops_payload(*stops: tuple[str, str]) -> dict:
    return {
        "data": [{"type": "stop", "id": sid, "attributes": {"name": name}} for sid, name in stops]
    }


def future_predictions(*offsets_min: float) -> dict:
    now = datetime.now(UTC)
    return {
        "data": [
            {
                "type": "prediction",
                "id": str(i),
                "attributes": {
                    "departure_time": (now + timedelta(minutes=m)).isoformat(),
                    "arrival_time": None,
                },
            }
            for i, m in enumerate(offsets_min)
        ]
    }


def build(respx_mock, router: FakeRouter, store=None):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json=ROUTES_PAYLOAD)
    )
    # Predictions now weave in a service heads-up, so they make an extra /alerts call.
    # Default it to "no alerts"; a test that wants a real alert re-mocks /alerts AFTER
    # build() (same respx route -> the later .mock() wins).
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = MBTAClient(base_url=MBTA_BASE_URL)
    resolver = Resolver(client, RouteCache(client))
    departures = DeparturesService(client)
    graph = build_graph(router=router, resolver=resolver, departures=departures, store=store)
    return graph, client


def config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


async def test_disambiguation_resolves_on_next_turn(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=future_predictions(5, 13))
    )
    router = FakeRouter(
        {
            "next 116 from Maverick": RouterSlots(
                intent="predictions", route="116", location="Maverick"
            ),
            "toward Wonderland": RouterSlots(
                intent="predictions", direction_hint="Wonderland", follow_up=True
            ),
        }
    )
    graph, client = build(respx_mock, router)

    turn1 = await graph.ainvoke({"message": "next 116 from Maverick"}, config("s1"))
    assert isinstance(turn1["result"], Disambiguation)
    assert turn1["needs_input"] is True
    assert turn1["reply"].startswith("Which direction —")

    turn2 = await graph.ainvoke({"message": "toward Wonderland"}, config("s1"))
    assert isinstance(turn2["result"], PredictionResult)
    assert turn2["needs_input"] is False
    assert "116 from Maverick Station toward Wonderland" in turn2["reply"]
    assert "Which direction" not in turn2["reply"]
    await client.aclose()


async def test_follow_up_reuses_prior_slots(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=future_predictions(5, 13, 21))
    )
    router = FakeRouter(
        {
            "next 116 from Maverick toward Wonderland": RouterSlots(
                intent="predictions", route="116", location="Maverick", direction_hint="Wonderland"
            ),
            "and the one after?": RouterSlots(intent="predictions", follow_up=True),
        }
    )
    graph, client = build(respx_mock, router)

    turn1 = await graph.ainvoke(
        {"message": "next 116 from Maverick toward Wonderland"}, config("s2")
    )
    assert isinstance(turn1["result"], PredictionResult)

    # Follow-up carries no route/location/direction; it must reuse the last target.
    turn2 = await graph.ainvoke({"message": "and the one after?"}, config("s2"))
    assert isinstance(turn2["result"], PredictionResult)
    assert turn2["result"].target.route_id == "116"
    assert turn2["result"].target.direction_destination == "Wonderland"
    assert "116 from Maverick Station toward Wonderland" in turn2["reply"]
    await client.aclose()


async def test_sessions_are_isolated(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    router = FakeRouter(
        {
            "next 116 from Maverick": RouterSlots(
                intent="predictions", route="116", location="Maverick"
            ),
            "and the one after?": RouterSlots(intent="predictions", follow_up=True),
        }
    )
    graph, client = build(respx_mock, router)

    # Session A asks an ambiguous question (leaves a pending disambiguation in A).
    turn_a = await graph.ainvoke({"message": "next 116 from Maverick"}, config("A"))
    assert isinstance(turn_a["result"], Disambiguation)  # A is mid-clarification

    # Session B's follow-up shares no memory with A: no pending, no last_target.
    # So it can't reuse anything and must ask which route — proving isolation.
    turn_b = await graph.ainvoke({"message": "and the one after?"}, config("B"))
    assert isinstance(turn_b["result"], Disambiguation)
    assert turn_b["result"].kind.value == "route"
    assert "Which route" in turn_b["reply"]
    await client.aclose()


async def test_fresh_query_escapes_a_pending_clarification(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=future_predictions(5))
    )
    router = FakeRouter(
        {
            # Turn 1: missing direction -> leaves a DIRECTION clarification pending.
            "next 116 from Maverick": RouterSlots(
                intent="predictions", route="116", location="Maverick"
            ),
            # Turn 2: a self-contained new query (route + location) -> must NOT be
            # swallowed as an answer; it resolves on its own.
            "actually next 116 from Maverick toward Wonderland": RouterSlots(
                intent="predictions", route="116", location="Maverick", direction_hint="Wonderland"
            ),
        }
    )
    graph, client = build(respx_mock, router)

    turn1 = await graph.ainvoke({"message": "next 116 from Maverick"}, config("esc"))
    assert isinstance(turn1["result"], Disambiguation)

    turn2 = await graph.ainvoke(
        {"message": "actually next 116 from Maverick toward Wonderland"}, config("esc")
    )
    assert isinstance(turn2["result"], PredictionResult)
    assert turn2["needs_input"] is False
    assert "116 from Maverick Station toward Wonderland" in turn2["reply"]
    await client.aclose()


async def test_alerts_intent_returns_alerts(respx_mock):
    router = FakeRouter({"is the Blue Line down?": RouterSlots(intent="alerts", route="Blue Line")})
    graph, client = build(respx_mock, router)
    # Re-mock /alerts after build() so this content wins over build()'s empty default.
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "alert",
                        "id": "1",
                        "attributes": {
                            "effect": "DELAY",
                            "severity": 5,
                            "header": "Blue Line delays of about 10 minutes.",
                            "short_header": "Blue Line delays of about 10 minutes.",
                        },
                    }
                ]
            },
        )
    )
    turn = await graph.ainvoke({"message": "is the Blue Line down?"}, config("s3"))
    assert "Blue Line delays of about 10 minutes." in turn["reply"]
    assert turn["needs_input"] is False
    await client.aclose()


async def test_follow_up_one_after_returns_later_departures(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=future_predictions(4, 12, 19, 27))
    )
    router = FakeRouter(
        {
            "next 116 from Maverick toward Wonderland": RouterSlots(
                intent="predictions", route="116", location="Maverick", direction_hint="Wonderland"
            ),
            "and the one after that?": RouterSlots(intent="predictions", follow_up=True, offset=1),
        }
    )
    graph, client = build(respx_mock, router)

    turn1 = await graph.ainvoke(
        {"message": "next 116 from Maverick toward Wonderland"}, config("paging")
    )
    turn2 = await graph.ainvoke({"message": "and the one after that?"}, config("paging"))

    assert isinstance(turn1["result"], PredictionResult)
    assert isinstance(turn2["result"], PredictionResult)
    # The offset slot flowed router -> state -> predictions and advanced the window.
    assert turn2["result"].minutes_away[0] > turn1["result"].minutes_away[0]
    await client.aclose()


class RecordingRouter:
    """A fake router that records the history handed to it each turn."""

    def __init__(self, mapping: dict[str, RouterSlots]):
        self._mapping = mapping
        self.seen_history: list = []

    async def route(self, message: str, *, history=None) -> RouterSlots:
        self.seen_history.append(history)
        return self._mapping[message]


async def test_router_receives_prior_turns_as_history(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=future_predictions(5, 13))
    )
    router = RecordingRouter(
        {
            "next 116 from Maverick toward Wonderland": RouterSlots(
                intent="predictions", route="116", location="Maverick", direction_hint="Wonderland"
            ),
            "and the one after?": RouterSlots(intent="predictions", follow_up=True),
        }
    )
    graph, client = build(respx_mock, router)

    await graph.ainvoke({"message": "next 116 from Maverick toward Wonderland"}, config("hist"))
    await graph.ainvoke({"message": "and the one after?"}, config("hist"))

    assert not router.seen_history[0]  # first turn: no prior context
    contents = [m["content"] for m in router.seen_history[1]]
    assert "next 116 from Maverick toward Wonderland" in contents  # turn 2 sees turn 1
    await client.aclose()


async def test_new_question_escapes_stale_clarification(respx_mock):
    router = FakeRouter(
        {
            # No stop -> predictions leaves a "which stop?" clarification pending.
            "next blue line": RouterSlots(intent="predictions", route="Blue Line"),
            # A new, non-follow-up question must escape that pending, not be swallowed.
            "is the blue line running": RouterSlots(intent="alerts", route="Blue Line"),
        }
    )
    graph, client = build(respx_mock, router)
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(200, json=one_alert("DELAY", 5, "Blue Line minor delays."))
    )

    turn1 = await graph.ainvoke({"message": "next blue line"}, config("esc2"))
    assert isinstance(turn1["result"], Disambiguation)  # "Which stop did you mean?"

    turn2 = await graph.ainvoke({"message": "is the blue line running"}, config("esc2"))
    assert "Blue Line minor delays." in turn2["reply"]  # escaped into alerts
    assert turn2["needs_input"] is False
    await client.aclose()


def one_alert(effect: str, severity: int, header: str) -> dict:
    return {
        "data": [
            {
                "type": "alert",
                "id": "1",
                "attributes": {
                    "effect": effect,
                    "severity": severity,
                    "header": header,
                    "short_header": header,
                },
            }
        ]
    }


async def test_alerts_clarifies_missing_route_across_turns(respx_mock):
    router = FakeRouter(
        {
            "any alerts?": RouterSlots(intent="alerts"),
            "the Blue Line": RouterSlots(intent="alerts", route="Blue Line", follow_up=True),
        }
    )
    graph, client = build(respx_mock, router)
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(200, json=one_alert("DELAY", 5, "Blue Line minor delays."))
    )

    turn1 = await graph.ainvoke({"message": "any alerts?"}, config("al"))
    assert turn1["needs_input"] is True
    assert "Which route" in turn1["reply"]

    # The answer alone resolves — the skill remembered it was waiting on a route.
    turn2 = await graph.ainvoke({"message": "the Blue Line"}, config("al"))
    assert "Blue Line minor delays." in turn2["reply"]
    assert turn2["needs_input"] is False
    await client.aclose()


async def test_facilities_clarifies_missing_scope_across_turns(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("place-pktrm", "Park Street")))
    )
    router = FakeRouter(
        {
            "are the elevators working?": RouterSlots(intent="facilities"),
            "Park Street": RouterSlots(intent="facilities", location="Park Street", follow_up=True),
        }
    )
    graph, client = build(respx_mock, router)
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200, json=one_alert("ELEVATOR_CLOSURE", 7, "Park Street elevator unavailable.")
        )
    )

    turn1 = await graph.ainvoke({"message": "are the elevators working?"}, config("fac"))
    assert turn1["needs_input"] is True
    assert "Which station or line" in turn1["reply"]

    turn2 = await graph.ainvoke({"message": "Park Street"}, config("fac"))
    assert "Park Street elevator unavailable." in turn2["reply"]
    assert turn2["needs_input"] is False
    await client.aclose()


async def test_unknown_intent_routes_to_fallback(respx_mock):
    router = FakeRouter({"what's the weather?": RouterSlots(intent="unknown")})
    graph, client = build(respx_mock, router)
    turn = await graph.ainvoke({"message": "what's the weather?"}, config("s4"))
    assert "next bus or train" in turn["reply"]
    await client.aclose()


async def test_replies_have_no_ids_or_json(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=future_predictions(5))
    )
    router = FakeRouter(
        {
            "next 116 from Maverick toward Wonderland": RouterSlots(
                intent="predictions", route="116", location="Maverick", direction_hint="Wonderland"
            )
        }
    )
    graph, client = build(respx_mock, router)
    turn = await graph.ainvoke(
        {"message": "next 116 from Maverick toward Wonderland"}, config("s5")
    )
    reply = turn["reply"]
    assert "{" not in reply and "}" not in reply
    assert "70" not in reply  # no stop_id
    assert "_id" not in reply
    await client.aclose()


async def test_save_commute_then_leave_now_end_to_end(respx_mock, tmp_path):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json=stops_payload(("70", "Maverick Station")))
    )
    # Next vehicles in 10 and 20 min; a 5-min walk -> leave in 5, then 15.
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=future_predictions(10, 20))
    )
    save_msg = "save the 116 from Maverick toward Wonderland as morning, 5 min walk"
    router = FakeRouter(
        {
            save_msg: RouterSlots(
                intent="save_commute",
                route="116",
                location="Maverick",
                direction_hint="Wonderland",
                commute_name="morning",
                walk_minutes=5,
            ),
            "should I leave now for my morning commute?": RouterSlots(
                intent="leave_now", commute_name="morning"
            ),
        }
    )
    store = CommuteStore(tmp_path / "dexter.db")
    await store.init()
    graph, client = build(respx_mock, router, store=store)

    # Turn 1: save (carries the rider's opaque user_id), turn 2: ask when to leave.
    turn1 = await graph.ainvoke(
        {"message": save_msg, "user_id": "rider-1"},
        config("commute"),
    )
    assert "Saved your morning commute" in turn1["reply"]
    assert await store.get("rider-1", "morning") is not None  # actually persisted

    turn2 = await graph.ainvoke(
        {"message": "should I leave now for my morning commute?", "user_id": "rider-1"},
        config("commute"),
    )
    # Exact leave-in math is unit-tested in test_formatting; here just confirm the
    # leave-now wiring resolved the saved commute and produced a leave-in reply.
    reply2 = turn2["reply"]
    assert reply2.startswith("Leave in ")
    assert "to catch the 116 from Maverick Station toward Wonderland" in reply2
    assert "for the one after" in reply2
    await client.aclose()


async def test_leave_now_without_saved_commute_guides_user(respx_mock, tmp_path):
    router = FakeRouter(
        {"should I leave now?": RouterSlots(intent="leave_now")},
    )
    store = CommuteStore(tmp_path / "dexter.db")
    await store.init()
    graph, client = build(respx_mock, router, store=store)
    turn = await graph.ainvoke(
        {"message": "should I leave now?", "user_id": "new-rider"}, config("nocommute")
    )
    assert "haven't saved a commute yet" in turn["reply"]
    await client.aclose()
