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

INTENTS = (
    "predictions",
    "leave_now",
    "save_commute",
    "alerts",
    "service_status",
    "facilities",
    "smalltalk",
    "unknown",
)
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
- "leave_now": the rider asks when to LEAVE for a saved commute, or whether to go now \
("should I leave now?", "when should I leave for work?", "do I have time?"). Often names a \
saved commute ("leave now for my morning commute") — put that in commute_name.
- "save_commute": the rider asks to SAVE or remember a trip as a named commute \
("save this as my morning commute", "remember the 116 from Bennington to Maverick as work, \
5 minute walk"). Put the label in commute_name and any stated walk time in walk_minutes.
- "alerts": service alerts, delays, or disruptions for ONE named route or line \
("is the Blue Line down?", "any delays on the 66?").
- "service_status": the health of the whole system, when NO specific route is named \
now OR carried from the conversation ("how's the T right now?", "any delays anywhere?", \
"is everything running ok?", "what's down right now?"). If a route is named now or still in \
play from an earlier turn, use "alerts" instead — e.g. after "any alerts on the Red Line?", \
a bare "is it running?" is "alerts" with route "Red Line", not "service_status".
- "facilities": elevators or escalators ("is the elevator at Park St working?").
- "smalltalk": social conversation, not a request for transit info — greetings ("hi"), \
thanks ("thank you"), acknowledgements or sign-offs ("no that's enough", "that's all", \
"ok cool", "bye"). Prefer this over "unknown" for anything conversational.
- "unknown": an actual question or request that isn't about MBTA transit \
("what's the weather?", "tell me a joke").

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
1 for "and the one after that?", 2 for "two after that"; 0 (or omit) otherwise.
- commute_name: the rider's label for a saved commute ("morning", "work", "home"), for \
save_commute and leave_now. Omit if they didn't name one.
- walk_minutes: how many minutes the rider says it takes to walk to the stop, for save_commute \
("5 minute walk" -> 5). Omit if not stated."""

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
                "commute_name": {
                    "type": "string",
                    "description": "Saved-commute label, e.g. morning or work.",
                },
                "walk_minutes": {
                    "type": "integer",
                    "description": "Stated walk time to the stop, in minutes.",
                },
            },
            "required": ["intent"],
        },
    },
}


SMALLTALK_PROMPT = """You are Dexter, a warm and concise assistant for the MBTA (Boston) transit \
system. The user said something conversational — a greeting, thanks, or a sign-off — not a transit \
question. Reply in ONE short, friendly, natural sentence:
- If they greeted you, greet them back and invite them to ask about the next bus or train.
- If they thanked you or are wrapping up, acknowledge warmly and let them know you're around.
Never state arrival times, schedules, routes, or any other transit facts. Plain text, no emojis."""

# Used only if the smalltalk model call fails — never leave a social turn unanswered.
DEFAULT_SMALLTALK = "Hi! Ask me when the next bus or train is coming."


@dataclass(frozen=True, slots=True)
class RouterSlots:
    intent: str
    route: str | None = None
    location: str | None = None
    direction_hint: str | None = None
    follow_up: bool = False
    offset: int = 0
    commute_name: str | None = None
    walk_minutes: int | None = None


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

    async def smalltalk(self, message: str, *, history: list[dict] | None = None) -> str:
        """Generate a brief, natural reply to a social (non-transit) message.

        Safe to let the model write freely here: the prompt forbids transit facts,
        and this is only invoked for the ``smalltalk`` intent — all real departure
        info still comes from templates, never the LLM.
        """
        messages: list[dict] = [{"role": "system", "content": SMALLTALK_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        response = await self._client.chat.completions.create(
            model=self._deployment,
            messages=messages,
            temperature=0.6,  # a little warmth/variety; no facts at stake
            max_completion_tokens=60,
        )
        try:
            text = (response.choices[0].message.content or "").strip()
        except (AttributeError, IndexError, TypeError):
            return DEFAULT_SMALLTALK
        return text or DEFAULT_SMALLTALK


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
        commute_name=_clean(args.get("commute_name")),
        walk_minutes=_parse_walk_minutes(args.get("walk_minutes")),
    )


def _parse_walk_minutes(value) -> int | None:
    """A non-negative walk time in minutes; None when absent or unparseable."""
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


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
