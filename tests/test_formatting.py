"""Milestone 5 — speakable formatting (templated; no LLM, no IDs, no JSON)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dexter.agent.formatting import format_node, format_outcome
from dexter.agent.state import Fallback, ServiceError, SkillUnavailable
from dexter.mbta.models import (
    Disambiguation,
    DisambiguationKind,
    DisambiguationOption,
    NoServiceResult,
    PredictionResult,
    ResolvedTarget,
    ScheduleResult,
)

EASTERN = ZoneInfo("America/New_York")


def target(dest="Maverick", route_name="116", route_type=3, stop="Bennington St @ Brooks St"):
    return ResolvedTarget(
        route_id="116",
        route_name=route_name,
        stop_ids=("5740",),
        stop_name=stop,
        direction_id=1,
        direction_destination=dest,
        route_type=route_type,
    )


def test_predictions_three_departures():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(4, 12, 19)))
    assert text == "The next 116 toward Maverick is in 4 minutes, then 12 and 19 minutes."


def test_predictions_two_departures():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(4, 12)))
    assert text == "The next 116 toward Maverick is in 4 minutes, then 12 minutes."


def test_predictions_single_departure_and_one_minute():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(1,)))
    assert text == "The next 116 toward Maverick is in 1 minute."


def test_predictions_arriving_now():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(0, 7)))
    assert text == "The next 116 toward Maverick is arriving now, then 7 minutes."


def test_schedule_clock_time():
    when = datetime(2026, 6, 6, 23, 42, tzinfo=EASTERN)
    text = format_outcome(ScheduleResult(target=target(), next_time=when))
    assert text == (
        "Real-time data isn't available, but per the schedule the next "
        "116 toward Maverick should come around 11:42 PM."
    )


def test_schedule_strips_leading_zero_hour():
    when = datetime(2026, 6, 6, 21, 5, tzinfo=EASTERN)  # 9:05 PM
    text = format_outcome(ScheduleResult(target=target(), next_time=when))
    assert "around 9:05 PM." in text


def test_no_service_uses_bus_for_bus_route():
    text = format_outcome(NoServiceResult(target=target(route_type=3)))
    assert text == "There appear to be no 116 buses toward Maverick around you right now."


def test_no_service_uses_train_for_subway():
    text = format_outcome(
        NoServiceResult(target=target(route_name="Blue Line", route_type=1, dest="Wonderland"))
    )
    assert "no Blue Line trains toward Wonderland" in text


def test_direction_disambiguation_question():
    disambiguation = Disambiguation(
        kind=DisambiguationKind.DIRECTION,
        options=(
            DisambiguationOption(label="Maverick", direction_id=1),
            DisambiguationOption(label="Wonderland", direction_id=0),
        ),
    )
    text = format_outcome(disambiguation)
    assert text == "Which direction — toward Maverick or toward Wonderland?"


def test_stop_disambiguation_replaces_at_symbol():
    disambiguation = Disambiguation(
        kind=DisambiguationKind.STOP,
        options=(
            DisambiguationOption(label="Bennington St @ Brooks St", stop_ids=("1",)),
            DisambiguationOption(label="Bennington St @ Boardman St", stop_ids=("2",)),
        ),
    )
    text = format_outcome(disambiguation)
    assert "@" not in text
    assert "Bennington St at Brooks St" in text
    assert "Bennington St at Boardman St" in text


def test_empty_route_disambiguation_is_generic():
    text = format_outcome(Disambiguation(kind=DisambiguationKind.ROUTE))
    assert text.startswith("Which route did you mean")


def test_skill_unavailable_alerts():
    text = format_outcome(SkillUnavailable(skill="alerts"))
    assert text == "I can't check service alerts yet — that's coming in a later version."


def test_service_error_busy_and_unavailable():
    assert "busy" in format_outcome(ServiceError(kind="busy"))
    assert "couldn't reach" in format_outcome(ServiceError(kind="unavailable"))


def test_fallback_is_helpful():
    text = format_outcome(Fallback())
    assert "next bus or train" in text


def test_format_node_adds_reclarify_prefix_and_resets_flag():
    direction = Disambiguation(
        kind=DisambiguationKind.DIRECTION,
        options=(DisambiguationOption(label="Maverick", direction_id=1),),
    )
    update = format_node({"result": direction, "reclarify": True})
    assert update["reply"].startswith("Sorry, I didn't catch that. Which direction")
    assert update["reclarify"] is False  # flag cleared after use


def test_no_ids_or_json_in_outputs():
    samples = [
        format_outcome(PredictionResult(target=target(), minutes_away=(4, 12))),
        format_outcome(NoServiceResult(target=target())),
        format_outcome(
            Disambiguation(
                kind=DisambiguationKind.STOP,
                options=(
                    DisambiguationOption(label="Bennington St @ Brooks St", stop_ids=("5740",)),
                ),
            )
        ),
    ]
    for text in samples:
        assert "{" not in text and "}" not in text
        assert "5740" not in text  # no stop_id leakage
        assert "_id" not in text
