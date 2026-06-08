"""Structured outcomes -> short, speakable text (PRD §6.5).

Every reply is built from templates here. **Times are templated from API data,
never produced by the LLM.** No stop IDs, no route IDs, no JSON — only
human-readable names, relative minutes, and clock times.
"""

from __future__ import annotations

from datetime import datetime

from dexter.mbta._timeutils import MBTA_TZ
from dexter.mbta.models import (
    Alert,
    AlertsResult,
    Disambiguation,
    DisambiguationKind,
    FacilitiesResult,
    NoServiceResult,
    PredictionResult,
    ResolvedTarget,
    ScheduleResult,
    StopNotOnRoute,
    SystemStatusResult,
)

from .state import AgentState, ServiceError, SkillUnavailable, SmallTalk

_VEHICLE_PLURAL = {0: "trains", 1: "trains", 2: "trains", 3: "buses", 4: "ferries"}


# Recent turns fed back to the router so it can resolve "it" / "what about …" /
# follow-ups. Capped to keep the router prompt small (latency) — ~3 exchanges.
MAX_HISTORY_MESSAGES = 6


def format_node(state: AgentState) -> dict:
    """Render ``state['result']`` into ``state['reply']``, and record the turn in history.

    `router_node` runs first each turn and reads the *prior* turns; this end-of-turn
    append records the current one for next time, so the router never sees the live
    message twice (it appends that itself).
    """
    reply = format_outcome(state.get("result"))
    if state.get("reclarify"):
        reply = "Sorry, I didn't catch that. " + reply
    history = list(state.get("history") or [])
    history.append({"role": "user", "content": state.get("message", "")})
    history.append({"role": "assistant", "content": reply})
    return {"reply": reply, "reclarify": False, "history": history[-MAX_HISTORY_MESSAGES:]}


def format_outcome(outcome) -> str:
    match outcome:
        case PredictionResult():
            return _format_predictions(outcome)
        case ScheduleResult():
            return _format_schedule(outcome)
        case NoServiceResult():
            return _format_no_service(outcome)
        case StopNotOnRoute():
            return _format_stop_not_on_route(outcome)
        case AlertsResult():
            return _format_alerts(outcome)
        case SystemStatusResult():
            return _format_system_status(outcome)
        case FacilitiesResult():
            return _format_facilities(outcome)
        case Disambiguation():
            return _format_disambiguation(outcome)
        case SkillUnavailable():
            return _format_skill_unavailable(outcome)
        case ServiceError():
            return _format_service_error(outcome)
        case SmallTalk():
            return outcome.text  # already a complete, model-written reply
        case _:
            return _format_fallback()


# --- predictions ------------------------------------------------------------


def _format_predictions(result: PredictionResult) -> str:
    minutes = result.minutes_away
    descriptor = _target_descriptor(result.target)
    if not minutes:
        # Only reached by paging past the last departure ("the one after that").
        return f"That's the last {descriptor} I can see right now."

    sentence = f"The next {descriptor} is {_relative_lead(minutes[0])}"
    rest = minutes[1:]
    if rest:
        sentence += f", then {_join_minutes(rest)}"
    return _with_heads_up(sentence + ".", result.alert)


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
    sentence = (
        "Real-time data isn't available, but per the schedule the next "
        f"{descriptor} should come around {_clock(result.next_time)}."
    )
    return _with_heads_up(sentence, result.alert)


def _format_no_service(result: NoServiceResult) -> str:
    target = result.target
    vehicles = _VEHICLE_PLURAL.get(target.route_type, "trips")
    toward = f" toward {target.direction_destination}" if target.direction_destination else ""
    sentence = f"There appear to be no {target.route_name} {vehicles}{toward} around you right now."
    return _with_heads_up(sentence, result.alert)


def _clock(when: datetime) -> str:
    local = when.astimezone(MBTA_TZ)
    return local.strftime("%I:%M %p").lstrip("0")


# --- stop not on this route -------------------------------------------------

# Silver Line is branded rapid transit but runs as buses (these route ids).
_SILVER_LINE_IDS = frozenset({"741", "742", "743", "746", "749", "751"})


def _format_stop_not_on_route(outcome: StopNotOnRoute) -> str:
    stop = _speakable(outcome.stop_name)
    route = _speakable(outcome.route_label)
    served = _served_modes(outcome.served_by)
    if served:
        return f"{stop} isn't on the {route} — it's served by the {_join_and(served)}."
    return f"{stop} isn't on the {route}."


def _served_modes(routes) -> list[str]:
    """Group serving routes into speakable modes; collapse a long bus list to 'local buses'."""
    modes: list[str] = []
    buses: list[str] = []

    def add(label: str) -> None:
        if label not in modes:
            modes.append(label)

    for route in routes:
        if route.type == 2:  # commuter rail
            add("Commuter Rail")
        elif route.id in _SILVER_LINE_IDS or (route.short_name or "").upper().startswith("SL"):
            add("Silver Line")
        elif route.id.startswith("Green-"):
            add("Green Line")
        elif route.type in (0, 1):  # subway / light rail
            add(route.display_name)  # "Red Line", "Blue Line"
        else:  # a regular bus route
            buses.append(route.display_name)

    if len(buses) == 1:
        add(buses[0])
    elif buses:
        add("local buses")
    return modes


def _join_and(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


# --- alerts / facilities ----------------------------------------------------

# Raw MBTA effect -> a short, speakable lead used when an alert has no header text.
_EFFECT_PHRASING = {
    "SUSPENSION": "service is suspended",
    "SHUTTLE": "shuttle buses are replacing service",
    "STATION_CLOSURE": "a station is closed",
    "STOP_CLOSURE": "a stop is closed",
    "STATION_ISSUE": "there's a station issue",
    "DETOUR": "there's a detour",
    "DELAY": "there are delays",
    "SERVICE_CHANGE": "there's a service change",
    "SCHEDULE_CHANGE": "there's a schedule change",
    "TRACK_CHANGE": "there's a track change",
    "SNOW_ROUTE": "snow routing is in effect",
    "ELEVATOR_CLOSURE": "an elevator is out of service",
    "ESCALATOR_CLOSURE": "an escalator is out of service",
}

# How many individual alerts we'll read out before summarizing the rest.
_MAX_SPOKEN_ALERTS = 2


def _format_alerts(result: AlertsResult) -> str:
    scope = _speakable(result.scope_label)
    if not result.alerts:
        return f"The {scope} is running normally — no current service alerts."

    spoken = [_alert_sentence(a) for a in result.alerts[:_MAX_SPOKEN_ALERTS]]
    text = " ".join(spoken)
    remaining = len(result.alerts) - len(spoken)
    if remaining > 0:
        text += f" There {'is' if remaining == 1 else 'are'} {remaining} more alert"
        text += "." if remaining == 1 else "s."
    return text


def _format_facilities(result: FacilitiesResult) -> str:
    scope = _speakable(result.scope_label)
    if not result.outages:
        return f"There are no elevator or escalator outages for the {scope} right now."
    spoken = [_alert_sentence(o) for o in result.outages[:_MAX_SPOKEN_ALERTS]]
    text = " ".join(spoken)
    remaining = len(result.outages) - len(spoken)
    if remaining > 0:
        text += f" There {'is' if remaining == 1 else 'are'} {remaining} more outage"
        text += "." if remaining == 1 else "s."
    return text


def _alert_sentence(alert: Alert) -> str:
    """Prefer the MBTA's human-written header; fall back to effect phrasing."""
    if alert.header:
        return _ensure_period(_speakable(alert.header))
    phrase = _EFFECT_PHRASING.get(alert.effect, "there's a service alert")
    return _ensure_period(phrase[0].upper() + phrase[1:])


def _ensure_period(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


# --- service-aware predictions (one-line heads-up) --------------------------


def _with_heads_up(sentence: str, alert: Alert | None) -> str:
    """Append a single short heads-up about an active disruption, if there is one."""
    if alert is None:
        return sentence
    phrase = _EFFECT_PHRASING.get(alert.effect, "there's a service alert")
    return f"{sentence} Heads up — {phrase}."


# --- system status ("how's the T right now?") -------------------------------

# Raw MBTA effect -> a verb phrase that reads after a line name ("the Red Line ...").
_LINE_EFFECT_PHRASING = {
    "SUSPENSION": "is suspended",
    "SHUTTLE": "has shuttle buses replacing service",
    "STATION_CLOSURE": "has a station closed",
    "STOP_CLOSURE": "has a stop closed",
    "STATION_ISSUE": "has a station issue",
    "DETOUR": "is on a detour",
    "DELAY": "has delays",
    "SERVICE_CHANGE": "has a service change",
    "SCHEDULE_CHANGE": "has a schedule change",
    "TRACK_CHANGE": "has a track change",
    "SNOW_ROUTE": "is on snow routing",
}


def _format_system_status(result: SystemStatusResult) -> str:
    if not result.affected:
        return "Good news — the whole system is running normally right now."
    parts = [
        f"the {_speakable(line.label)} {_line_status_phrase(line.alert)}"
        for line in result.affected
    ]
    body = _join_and(parts)
    return f"{body[0].upper()}{body[1:]}; everything else is running normally."


def _line_status_phrase(alert: Alert) -> str:
    return _LINE_EFFECT_PHRASING.get(alert.effect, "has a service alert")


# --- disambiguation ---------------------------------------------------------


def _format_disambiguation(disambiguation: Disambiguation) -> str:
    if disambiguation.kind == DisambiguationKind.DIRECTION:
        options = [f"toward {_speakable(o.label)}" for o in disambiguation.options]
        return "Which direction — " + _or_join(options) + "?"
    if disambiguation.kind == DisambiguationKind.STOP:
        if disambiguation.options:
            options = [_speakable(o.label) for o in disambiguation.options]
            return "Which stop did you mean — " + _or_join(options) + "?"
        if disambiguation.query:
            return (
                f"I couldn't find a stop matching '{_speakable(disambiguation.query)}'. "
                "Which stop did you mean?"
            )
        return "Which stop did you mean?"
    if disambiguation.kind == DisambiguationKind.FACILITY_SCOPE:
        return "Which station or line did you mean — for example, Park Street or the Red Line?"
    # ROUTE
    if disambiguation.options:
        options = [f"the {_speakable(o.label)}" for o in disambiguation.options]
        return "Which route — " + _or_join(options) + "?"
    return "Which route did you mean — a bus number like 116, or a line like the Blue Line?"


# --- agent outcomes ---------------------------------------------------------


def _format_skill_unavailable(outcome: SkillUnavailable) -> str:
    # Alerts and facilities are now implemented; this remains for any future
    # scaffolded-but-unbuilt skill.
    return "That isn't available yet — it's coming in a later version."


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
    # Name the boarding stop too, so the rider can see exactly which stop/direction
    # the answer is for (and catch a wrong stop immediately).
    parts = [target.route_name]
    if target.stop_name:
        parts.append(f"from {_speakable(target.stop_name)}")
    if target.direction_destination:
        parts.append(f"toward {target.direction_destination}")
    return " ".join(parts)


def _speakable(label: str) -> str:
    # Stop names like "Bennington St @ Brooks St" read better as "... at ...".
    return label.replace(" @ ", " at ").strip()


def _or_join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"
