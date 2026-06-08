"""Milestone 4 — router intent + slot extraction (LLM mocked).

The Azure client is faked, so these assert request shape (gpt-5-correct params)
and parsing of the tool call into slots — not the model's judgment (that's the
live smoke test / M6).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from dexter.agent.router import TOOL_NAME, Router, RouterSlots


def make_client(tool_args: dict | None = None, *, no_tool: bool = False, bad_json: bool = False):
    """Build a fake (Async)AzureOpenAI client and the captured completions object."""
    if no_tool:
        message = SimpleNamespace(tool_calls=None, content="hello")
    else:
        arguments = "{not json" if bad_json else json.dumps(tool_args)
        call = SimpleNamespace(function=SimpleNamespace(name=TOOL_NAME, arguments=arguments))
        message = SimpleNamespace(tool_calls=[call], content=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class Completions:
        def __init__(self):
            self.kwargs = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return response

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


async def test_extracts_predictions_slots_and_router_params():
    client, completions = make_client(
        {
            "intent": "predictions",
            "route": "116",
            "location": "Bennington Street",
            "direction_hint": "Maverick",
            "follow_up": False,
        }
    )
    slots = await Router(client, "gpt-4.1-mini-1234").route(
        "when's the next 116 from Bennington Street toward Maverick?"
    )

    assert slots == RouterSlots(
        intent="predictions",
        route="116",
        location="Bennington Street",
        direction_hint="Maverick",
        follow_up=False,
    )
    # Deterministic extraction with a small output cap.
    assert completions.kwargs["model"] == "gpt-4.1-mini-1234"
    assert completions.kwargs["temperature"] == 0.0
    assert completions.kwargs["max_completion_tokens"] == 512
    assert "max_tokens" not in completions.kwargs
    assert completions.kwargs["tool_choice"] == "required"


async def test_classifies_alerts():
    client, _ = make_client({"intent": "alerts", "route": "Blue Line"})
    slots = await Router(client, "gpt-5-mini").route("is the Blue Line down?")
    assert slots.intent == "alerts"
    assert slots.route == "Blue Line"


async def test_classifies_service_status():
    client, _ = make_client({"intent": "service_status"})
    slots = await Router(client, "gpt-4.1-mini").route("how's the T right now?")
    assert slots.intent == "service_status"  # accepted, not degraded to unknown
    assert slots.route is None


async def test_classifies_facilities():
    client, _ = make_client({"intent": "facilities", "location": "Park Street"})
    slots = await Router(client, "gpt-5-mini").route("is the elevator at Park Street working?")
    assert slots.intent == "facilities"


async def test_classifies_unknown():
    client, _ = make_client({"intent": "unknown"})
    slots = await Router(client, "gpt-5-mini").route("what's the weather?")
    assert slots.intent == "unknown"


async def test_follow_up_flag_parsed():
    client, _ = make_client({"intent": "predictions", "follow_up": True})
    slots = await Router(client, "gpt-5-mini").route("and the one after that?")
    assert slots.follow_up is True


async def test_offset_parsed_for_later_followup():
    client, _ = make_client({"intent": "predictions", "follow_up": True, "offset": 1})
    slots = await Router(client, "gpt-4.1-mini").route("and the one after that?")
    assert slots.offset == 1


async def test_offset_defaults_to_zero_and_bad_values_degrade():
    client, _ = make_client({"intent": "predictions", "route": "116"})
    assert (await Router(client, "gpt-4.1-mini").route("next 116")).offset == 0
    client, _ = make_client({"intent": "predictions", "offset": "lots"})
    assert (await Router(client, "gpt-4.1-mini").route("next 116")).offset == 0


async def test_blank_slots_become_none():
    client, _ = make_client(
        {"intent": "predictions", "route": "116", "location": "   ", "direction_hint": ""}
    )
    slots = await Router(client, "gpt-5-mini").route("next 116")
    assert slots.route == "116"
    assert slots.location is None
    assert slots.direction_hint is None


async def test_history_is_forwarded():
    client, completions = make_client({"intent": "predictions", "follow_up": True})
    history = [{"role": "assistant", "content": "Which direction — toward Maverick or Wonderland?"}]
    await Router(client, "gpt-5-mini").route("toward Maverick", history=history)
    sent = completions.kwargs["messages"]
    assert sent[0]["role"] == "system"
    assert history[0] in sent
    assert sent[-1] == {"role": "user", "content": "toward Maverick"}


async def test_no_tool_call_degrades_to_unknown():
    client, _ = make_client(no_tool=True)
    slots = await Router(client, "gpt-5-mini").route("hi")
    assert slots.intent == "unknown"


async def test_malformed_arguments_degrade_to_unknown():
    client, _ = make_client(bad_json=True)
    slots = await Router(client, "gpt-5-mini").route("next 116")
    assert slots.intent == "unknown"


async def test_unexpected_intent_value_degrades_to_unknown():
    client, _ = make_client({"intent": "weather"})
    slots = await Router(client, "gpt-5-mini").route("next 116")
    assert slots.intent == "unknown"
