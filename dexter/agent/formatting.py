"""Structured outcomes -> short, speakable text (PRD §6.5).

Every reply is built from templates here. **Times are templated from API data,
never produced by the LLM.** No stop IDs, no route IDs, no JSON — only
human-readable names, relative minutes, and clock times.
"""

from __future__ import annotations

from datetime import datetime

from dexter.mbta._timeutils import MBTA_TZ
from dexter.mbta.models import (
    Disambiguation,
    DisambiguationKind,
    NoServiceResult,
    PredictionResult,
    ResolvedTarget,
    ScheduleResult,
)

from .state import AgentState, ServiceError, SkillUnavailable

_VEHICLE_SINGULAR = {0: "train", 1: "train", 2: "train", 3: "bus", 4: "ferry"}
_VEHICLE_PLURAL = {0: "trains", 1: "trains", 2: "trains", 3: "buses", 4: "ferries"}


def format_node(state: AgentState) -> dict:
    """Graph node: render ``state['result']`` into ``state['reply']``."""
    reply = format_outcome(state.get("result"))
    if state.get("reclarify"):
        reply = "Sorry, I didn't catch that. " + reply
    return {"reply": reply, "reclarify": False}


def format_outcome(outcome) -> str:
    match outcome:
        case PredictionResult():
            return _format_predictions(outcome)
        case ScheduleResult():
            return _format_schedule(outcome)
        case NoServiceResult():
            return _format_no_service(outcome)
        case Disambiguation():
            return _format_disambiguation(outcome)
        case SkillUnavailable():
            return _format_skill_unavailable(outcome)
        case ServiceError():
            return _format_service_error(outcome)
        case _:
            return _format_fallback()


# --- predictions ------------------------------------------------------------


def _format_predictions(result: PredictionResult) -> str:
    minutes = result.minutes_away
    descriptor = _target_descriptor(result.target)
    if not minutes:
        vehicle = _VEHICLE_SINGULAR.get(result.target.route_type, "trip")
        return f"I don't have an upcoming {vehicle} for the {descriptor} right now."

    sentence = f"The next {descriptor} is {_relative_lead(minutes[0])}"
    rest = minutes[1:]
    if rest:
        sentence += f", then {_join_minutes(rest)}"
    return sentence + "."


def _relative_lead(minutes: int) -> str:
    if minutes <= 0:
        return "arriving now"
    if minutes == 1:
        return "in 1 minute"
    return f"in {minutes} minutes"


def _join_minutes(values: tuple[int, ...]) -> str:
    nums = [str(v) for v in values]
    if len(nums) == 1:
        joined = nums[0]
    elif len(nums) == 2:
        joined = f"{nums[0]} and {nums[1]}"
    else:
        joined = ", ".join(nums[:-1]) + f" and {nums[-1]}"
    return f"{joined} minutes"


# --- schedule / no service --------------------------------------------------


def _format_schedule(result: ScheduleResult) -> str:
    descriptor = _target_descriptor(result.target)
    return (
        "Real-time data isn't available, but per the schedule the next "
        f"{descriptor} should come around {_clock(result.next_time)}."
    )


def _format_no_service(result: NoServiceResult) -> str:
    target = result.target
    vehicles = _VEHICLE_PLURAL.get(target.route_type, "trips")
    toward = f" toward {target.direction_destination}" if target.direction_destination else ""
    return f"There appear to be no {target.route_name} {vehicles}{toward} around you right now."


def _clock(when: datetime) -> str:
    local = when.astimezone(MBTA_TZ)
    return local.strftime("%I:%M %p").lstrip("0")


# --- disambiguation ---------------------------------------------------------


def _format_disambiguation(disambiguation: Disambiguation) -> str:
    if disambiguation.kind == DisambiguationKind.DIRECTION:
        options = [f"toward {_speakable(o.label)}" for o in disambiguation.options]
        return "Which direction — " + _or_join(options) + "?"
    if disambiguation.kind == DisambiguationKind.STOP:
        if disambiguation.options:
            options = [_speakable(o.label) for o in disambiguation.options]
            return "Which stop did you mean — " + _or_join(options) + "?"
        return "Which stop did you mean?"
    # ROUTE
    if disambiguation.options:
        options = [f"the {_speakable(o.label)}" for o in disambiguation.options]
        return "Which route — " + _or_join(options) + "?"
    return "Which route did you mean — a bus number like 116, or a line like the Blue Line?"


# --- agent outcomes ---------------------------------------------------------

_SKILL_TEXT = {
    "alerts": "I can't check service alerts yet — that's coming in a later version.",
    "facilities": "I can't check elevators or escalators yet — that's coming in a later version.",
}


def _format_skill_unavailable(outcome: SkillUnavailable) -> str:
    return _SKILL_TEXT.get(
        outcome.skill, "That isn't available yet — it's coming in a later version."
    )


def _format_service_error(outcome: ServiceError) -> str:
    if outcome.kind == "busy":
        return "The transit feed is busy right now — please try again in a moment."
    return "I couldn't reach the MBTA feed right now. Please try again shortly."


def _format_fallback() -> str:
    return (
        "I can tell you when the next bus or train is coming. "
        "Try asking something like: when's the next 116 from Bennington Street toward Maverick?"
    )


# --- helpers ----------------------------------------------------------------


def _target_descriptor(target: ResolvedTarget) -> str:
    if target.direction_destination:
        return f"{target.route_name} toward {target.direction_destination}"
    return target.route_name


def _speakable(label: str) -> str:
    # Stop names like "Bennington St @ Brooks St" read better as "... at ...".
    return label.replace(" @ ", " at ").strip()


def _or_join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"
