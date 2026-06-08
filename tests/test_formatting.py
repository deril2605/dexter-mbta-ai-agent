"""Milestone 5 — speakable formatting (templated; no LLM, no IDs, no JSON)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dexter.agent.formatting import MAX_HISTORY_MESSAGES, format_node, format_outcome
from dexter.agent.state import Fallback, ServiceError, SkillUnavailable
from dexter.mbta.models import (
    Alert,
    AlertsResult,
    Disambiguation,
    DisambiguationKind,
    DisambiguationOption,
    FacilitiesResult,
    NoServiceResult,
    PredictionResult,
    ResolvedTarget,
    Route,
    ScheduleResult,
    StopNotOnRoute,
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
    assert text == (
        "The next 116 from Bennington St at Brooks St toward Maverick "
        "is in 4 minutes, then 12 and 19 minutes."
    )


def test_predictions_two_departures():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(4, 12)))
    assert text == (
        "The next 116 from Bennington St at Brooks St toward Maverick "
        "is in 4 minutes, then 12 minutes."
    )


def test_predictions_single_departure_and_one_minute():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(1,)))
    assert text == "The next 116 from Bennington St at Brooks St toward Maverick is in 1 minute."


def test_predictions_arriving_now():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(0, 7)))
    assert text == (
        "The next 116 from Bennington St at Brooks St toward Maverick "
        "is arriving now, then 7 minutes."
    )


def test_schedule_clock_time():
    when = datetime(2026, 6, 6, 23, 42, tzinfo=EASTERN)
    text = format_outcome(ScheduleResult(target=target(), next_time=when))
    assert text == (
        "Real-time data isn't available, but per the schedule the next "
        "116 from Bennington St at Brooks St toward Maverick should come around 11:42 PM."
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


def test_stop_not_found_names_the_query():
    text = format_outcome(Disambiguation(kind=DisambiguationKind.STOP, query="zxqw plaza"))
    assert "zxqw plaza" in text.lower()
    assert "which stop" in text.lower()


def _route(rid, *, short="", long="", rtype):
    return Route(
        id=rid,
        short_name=short,
        long_name=long,
        type=rtype,
        direction_names=(),
        direction_destinations=(),
    )


def test_stop_not_on_route_groups_served_modes():
    served = (
        _route("Red", long="Red Line", rtype=1),
        _route("741", short="SL1", long="Silver Line SL1", rtype=3),
        _route("CR-Worcester", long="Worcester Line", rtype=2),
    )
    text = format_outcome(
        StopNotOnRoute(stop_name="South Station", route_label="Green Line", served_by=served)
    )
    assert text == (
        "South Station isn't on the Green Line — it's served by the "
        "Red Line, Silver Line, and Commuter Rail."
    )


def test_stop_not_on_route_without_served_routes():
    text = format_outcome(StopNotOnRoute(stop_name="South Station", route_label="Green Line"))
    assert text == "South Station isn't on the Green Line."


def test_stop_not_on_route_collapses_many_buses():
    served = (
        _route("Green-C", short="C", long="Green Line C", rtype=0),
        _route("8", short="8", long="Route 8", rtype=3),
        _route("19", short="19", long="Route 19", rtype=3),
    )
    text = format_outcome(
        StopNotOnRoute(stop_name="Kenmore", route_label="Red Line", served_by=served)
    )
    assert text == "Kenmore isn't on the Red Line — it's served by the Green Line and local buses."


def test_skill_unavailable_is_generic():
    text = format_outcome(SkillUnavailable(skill="something-future"))
    assert "coming in a later version" in text


def test_alerts_reads_header_and_summarizes_rest():
    result = AlertsResult(
        scope_label="Blue Line",
        alerts=(
            Alert(
                header="Blue Line suspended between Airport and Bowdoin.",
                effect="SUSPENSION",
                severity=9,
            ),
            Alert(header="Minor delays on the Blue Line.", effect="DELAY", severity=3),
            Alert(header="Escalator work at State.", effect="ESCALATOR_CLOSURE", severity=1),
        ),
    )
    text = format_outcome(result)
    assert "Blue Line suspended between Airport and Bowdoin." in text
    assert "Minor delays on the Blue Line." in text
    assert "1 more alert" in text  # the third is summarized, not read out


def test_alerts_empty_says_running_normally():
    text = format_outcome(AlertsResult(scope_label="Red Line", alerts=()))
    assert "Red Line is running normally" in text
    assert "no current service alerts" in text


def test_predictions_empty_window_reads_as_last():
    # An empty PredictionResult only happens when paging past the last departure.
    text = format_outcome(PredictionResult(target=target(), minutes_away=()))
    assert text == (
        "That's the last 116 from Bennington St at Brooks St toward Maverick I can see right now."
    )


def test_alerts_without_header_falls_back_to_effect():
    text = format_outcome(
        AlertsResult(scope_label="116", alerts=(Alert(header="", effect="SUSPENSION", severity=9),))
    )
    assert "service is suspended" in text.lower()


def test_facilities_outage_reads_header():
    result = FacilitiesResult(
        scope_label="Park Street",
        outages=(
            Alert(
                header="Park Street elevator 123 is out of service.",
                effect="ELEVATOR_CLOSURE",
                severity=7,
            ),
        ),
    )
    text = format_outcome(result)
    assert "Park Street elevator 123 is out of service." in text


def test_facilities_empty_is_reassuring():
    text = format_outcome(FacilitiesResult(scope_label="Park Street", outages=()))
    assert "no elevator or escalator outages" in text
    assert "Park Street" in text


def test_service_error_busy_and_unavailable():
    assert "busy" in format_outcome(ServiceError(kind="busy"))
    assert "couldn't reach" in format_outcome(ServiceError(kind="unavailable"))


def test_fallback_is_helpful():
    text = format_outcome(Fallback())
    assert "next bus or train" in text


def test_smalltalk_is_warm_not_a_capability_pitch():
    # Closing/social chit-chat gets a brief acknowledgement, not the help blurb.
    text = format_outcome(Fallback(kind="smalltalk"))
    assert "Anytime" in text
    assert "Try asking" not in text


def test_format_node_records_turn_in_history_and_caps():
    prior = [{"role": "user", "content": "old"}] * MAX_HISTORY_MESSAGES
    update = format_node({"message": "next 116", "result": Fallback(), "history": prior})

    assert len(update["history"]) == MAX_HISTORY_MESSAGES  # capped
    assert update["history"][-2] == {"role": "user", "content": "next 116"}
    assert update["history"][-1]["role"] == "assistant"
    assert update["history"][-1]["content"] == update["reply"]


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
