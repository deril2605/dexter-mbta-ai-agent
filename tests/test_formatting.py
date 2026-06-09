"""Milestone 5 — speakable formatting (templated; no LLM, no IDs, no JSON)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dexter.agent.formatting import MAX_HISTORY_MESSAGES, format_node, format_outcome
from dexter.agent.state import (
    Fallback,
    LeaveNow,
    NoSavedCommute,
    SavedCommuteConfirmation,
    SaveNeedsTrip,
    ServiceError,
    SkillUnavailable,
    SmallTalk,
)
from dexter.mbta.models import (
    Alert,
    AlertsResult,
    Disambiguation,
    DisambiguationKind,
    DisambiguationOption,
    FacilitiesResult,
    LineStatus,
    NoServiceResult,
    PredictionResult,
    ResolvedTarget,
    Route,
    ScheduleResult,
    StopNotOnRoute,
    SystemStatusResult,
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


def test_smalltalk_renders_model_text_verbatim():
    # The model writes the social reply; the formatter passes it through unchanged.
    text = format_outcome(SmallTalk(text="Hey there! What route are you taking?"))
    assert text == "Hey there! What route are you taking?"


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


# --- service-aware predictions (heads-up) -----------------------------------


def test_predictions_append_heads_up_when_alert_present():
    result = PredictionResult(
        target=target(route_name="Orange Line", route_type=1, dest="Oak Grove"),
        minutes_away=(3, 9),
        alert=Alert(header="Orange Line delays.", effect="DELAY", severity=4),
    )
    text = format_outcome(result)
    assert "is in 3 minutes, then 9 minutes." in text
    assert text.endswith("Heads up — there are delays.")


def test_predictions_no_heads_up_when_no_alert():
    text = format_outcome(PredictionResult(target=target(), minutes_away=(4, 12)))
    assert "Heads up" not in text


def test_schedule_appends_heads_up():
    when = datetime(2026, 6, 6, 23, 42, tzinfo=EASTERN)
    result = ScheduleResult(
        target=target(),
        next_time=when,
        alert=Alert(header="", effect="DETOUR", severity=3),
    )
    text = format_outcome(result)
    assert "11:42 PM." in text
    assert text.endswith("Heads up — there's a detour.")


def test_no_service_appends_explanatory_heads_up():
    result = NoServiceResult(
        target=target(route_name="Blue Line", route_type=1, dest="Bowdoin"),
        alert=Alert(header="", effect="SUSPENSION", severity=9),
    )
    text = format_outcome(result)
    assert "no Blue Line trains toward Bowdoin" in text
    assert text.endswith("Heads up — service is suspended.")


# --- system status ----------------------------------------------------------


def test_system_status_summarizes_affected_lines():
    result = SystemStatusResult(
        affected=(
            LineStatus(label="Red Line", alert=Alert(header="", effect="DELAY", severity=5)),
            LineStatus(
                label="Orange Line", alert=Alert(header="", effect="SUSPENSION", severity=9)
            ),
        )
    )
    text = format_outcome(result)
    assert text == (
        "The Red Line has delays and the Orange Line is suspended; "
        "everything else is running normally."
    )


def test_system_status_single_line():
    result = SystemStatusResult(
        affected=(LineStatus(label="Green Line", alert=Alert(header="", effect="SHUTTLE")),)
    )
    text = format_outcome(result)
    assert text == (
        "The Green Line has shuttle buses replacing service; everything else is running normally."
    )


def test_system_status_all_normal():
    text = format_outcome(SystemStatusResult(affected=()))
    assert "whole system is running normally" in text


# --- saved commutes ---------------------------------------------------------


def test_saved_commute_confirmation():
    text = format_outcome(
        SavedCommuteConfirmation(
            name="morning",
            route_name="116",
            stop_name="Bennington St @ Brooks St",
            direction_destination="Maverick",
            walk_minutes=5,
        )
    )
    assert text == (
        "Saved your morning commute: the 116 from Bennington St at Brooks St "
        "toward Maverick, a 5-minute walk. Ask me when to leave anytime."
    )


def test_leave_now_subtracts_walk_time():
    # Vehicles at 7, 15, 23 min; 5-min walk -> leave in 2, then 10.
    result = PredictionResult(target=target(), minutes_away=(7, 15, 23))
    text = format_outcome(LeaveNow(name="morning", walk_minutes=5, departures=result))
    assert text == (
        "Leave in 2 minutes to catch the 116 from Bennington St at Brooks St toward Maverick, "
        "or in 10 minutes for the one after."
    )


def test_leave_now_says_leave_now_when_walk_zero_or_due():
    result = PredictionResult(target=target(), minutes_away=(5, 12))
    text = format_outcome(LeaveNow(name="work", walk_minutes=5, departures=result))
    assert text.startswith("Leave now to catch the 116")


def test_leave_now_warns_when_next_is_sooner_than_walk():
    # Next vehicle in 3 min but a 5-min walk -> you'd miss it.
    result = PredictionResult(target=target(), minutes_away=(3,))
    text = format_outcome(LeaveNow(name="morning", walk_minutes=5, departures=result))
    assert "sooner than your 5-minute walk" in text
    assert "miss it" in text


def test_leave_now_with_schedule_fallback():
    when = datetime(2026, 6, 6, 8, 0, tzinfo=EASTERN)
    result = ScheduleResult(target=target(), next_time=when)
    text = format_outcome(LeaveNow(name="morning", walk_minutes=5, departures=result))
    assert "scheduled for 8:00 AM" in text
    assert "leave by about 7:55 AM" in text


def test_leave_now_includes_alert_heads_up():
    result = PredictionResult(
        target=target(),
        minutes_away=(7, 15),
        alert=Alert(header="Delays.", effect="DELAY", severity=4),
    )
    text = format_outcome(LeaveNow(name="morning", walk_minutes=5, departures=result))
    assert text.endswith("Heads up — there are delays.")


def test_no_saved_commute_named_and_unnamed():
    named = format_outcome(NoSavedCommute(name="evening"))
    assert "saved as 'evening'" in named
    unnamed = format_outcome(NoSavedCommute())
    assert "haven't saved a commute yet" in unnamed


def test_save_needs_trip_prompts_for_trip():
    text = format_outcome(SaveNeedsTrip())
    assert "Tell me the trip first" in text


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
