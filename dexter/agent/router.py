"""Intent + slot extraction via Azure OpenAI tool-calling (PRD §6.2).

This is the ONLY LLM in Dexter. It classifies the message into
``predictions | alerts | facilities | unknown`` and extracts slots — it never
answers the question or produces any times.

Targets a small, **non-reasoning** model (gpt-4.1-mini) on Azure: Chat Completions
+ tool-calling, `temperature=0` for deterministic extraction, and a small
`max_completion_tokens` cap. We switched off gpt-5-mini because its reasoning
tokens dominated latency (~7s/turn) for what is a trivial extraction task. The
deployment name comes from config.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

INTENTS = ("predictions", "alerts", "facilities", "unknown")
TOOL_NAME = "extract_transit_query"

SYSTEM_PROMPT = """You are the intent router for Dexter, an MBTA (Boston) transit assistant.
Classify the user's latest message and extract slots by calling the tool \
`extract_transit_query`. Do not answer the question and never state any times — only extract.

Use the conversation so far to resolve references. When the latest message continues an \
earlier subject without repeating it, carry the route (and a direction still in play) from the \
earlier turn and set follow_up. A boarding stop is specific to one route, though: never reuse a \
stop from an earlier turn for a different route — leave location empty unless this message names \
one. Examples: after "is the Blue Line running?", "when's the next train to Government Center" \
means route "Blue Line"; but after asking about the Blue Line at Airport, "what about the 116 to \
Maverick" means route "116", direction_hint "Maverick", and no location.

intent:
- "predictions": when the next bus/train arrives or departs \
(e.g. "when's the next 116 from Bennington Street toward Maverick").
- "alerts": service alerts, delays, disruptions ("is the Blue Line down?").
- "facilities": elevators or escalators ("is the elevator at Park St working?").
- "unknown": anything not about MBTA transit.

slots (leave a slot out when it isn't present, unless it carries over from context above):
- route: the route the user means — as they said it ("116", "Blue Line", "Green Line B"), or \
carried from an earlier turn when they're clearly still talking about it.
- location: the stop or place they're departing from ("Bennington Street", "Maverick").
- direction_hint: a destination or direction they're heading \
("Maverick", "inbound", "toward Harvard", "to Government Center").
- follow_up: true when the message refers back to a previous turn — a continuation \
("and the one after?", "what about inbound?") or a short answer to a clarifying question \
("the 116", "toward Maverick", "Maverick Station").
- offset: for a follow-up asking for LATER departures, how many to advance past the next one — \
1 for "and the one after that?", 2 for "two after that"; 0 (or omit) otherwise."""

_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Extract the user's transit intent and slots.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": list(INTENTS)},
                "route": {"type": "string", "description": "Route as said, e.g. 116 or Blue Line."},
                "location": {"type": "string", "description": "Stop or place name."},
                "direction_hint": {"type": "string", "description": "Destination or direction."},
                "follow_up": {"type": "boolean"},
                "offset": {
                    "type": "integer",
                    "description": "Advance for a 'later departures' follow-up: "
                    "1 for 'the one after that', else 0.",
                },
            },
            "required": ["intent"],
        },
    },
}


@dataclass(frozen=True, slots=True)
class RouterSlots:
    intent: str
    route: str | None = None
    location: str | None = None
    direction_hint: str | None = None
    follow_up: bool = False
    offset: int = 0


class Router:
    """Wraps an (Async)AzureOpenAI client to extract intent + slots."""

    def __init__(
        self,
        client,
        deployment: str,
        *,
        temperature: float = 0.0,
        max_completion_tokens: int = 512,
    ) -> None:
        self._client = client
        self._deployment = deployment
        self._temperature = temperature
        self._max_completion_tokens = max_completion_tokens

    async def route(self, message: str, *, history: list[dict] | None = None) -> RouterSlots:
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        response = await self._client.chat.completions.create(
            model=self._deployment,
            messages=messages,
            tools=[_TOOL],
            tool_choice="required",  # one tool defined -> always our extractor
            temperature=self._temperature,
            max_completion_tokens=self._max_completion_tokens,
        )
        return _parse_response(response)


def _parse_response(response) -> RouterSlots:
    """Parse the tool call into slots; degrade to 'unknown' on any malformed output."""
    try:
        tool_calls = response.choices[0].message.tool_calls
    except (AttributeError, IndexError, TypeError):
        return RouterSlots(intent="unknown")
    if not tool_calls:
        return RouterSlots(intent="unknown")
    try:
        args = json.loads(tool_calls[0].function.arguments)
    except (AttributeError, json.JSONDecodeError, TypeError):
        return RouterSlots(intent="unknown")

    intent = args.get("intent")
    if intent not in INTENTS:
        intent = "unknown"
    return RouterSlots(
        intent=intent,
        route=_clean(args.get("route")),
        location=_clean(args.get("location")),
        direction_hint=_clean(args.get("direction_hint")),
        follow_up=bool(args.get("follow_up", False)),
        offset=_parse_offset(args.get("offset")),
    )


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_offset(value) -> int:
    """A non-negative advance for 'later departures' follow-ups; 0 on anything odd."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
