"""Milestone 3 — schedule fallback chain (PRD §5.5).

Empty predictions -> next scheduled departure; none remaining -> no service.
All HTTP is respx-mocked.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from dexter.mbta.client import MBTAClient
from dexter.mbta.models import NoServiceResult, PredictionResult, ScheduleResult
from dexter.mbta.predictions import DeparturesService

from .conftest import MBTA_BASE_URL, NOW, TARGET


def iso(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def schedule(departure: str | None = None, arrival: str | None = None, _id: str = "s") -> dict:
    return {
        "type": "schedule",
        "id": _id,
        "attributes": {"departure_time": departure, "arrival_time": arrival},
    }


def payload(*items: dict) -> dict:
    return {"data": list(items)}


async def test_empty_predictions_fall_back_to_next_schedule(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    schedules = respx_mock.get(f"{MBTA_BASE_URL}/schedules").mock(
        return_value=httpx.Response(
            200,
            json=payload(
                schedule(departure=iso(-20), _id="past"),  # before now -> skipped
                schedule(departure=iso(18), _id="next"),  # the next one
                schedule(departure=iso(48), _id="later"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, ScheduleResult)
    assert result.next_time == NOW + timedelta(minutes=18)
    assert result.target is TARGET

    # filter[date] is today's MBTA service date (Eastern).
    assert schedules.calls.last.request.url.params["filter[date]"] == "2026-06-06"


async def test_no_service_when_no_predictions_and_no_remaining_schedule(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get(f"{MBTA_BASE_URL}/schedules").mock(
        return_value=httpx.Response(
            200,
            json=payload(schedule(departure=iso(-30), _id="past")),  # only past service
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, NoServiceResult)
    assert result.target is TARGET


async def test_no_service_when_schedule_empty(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get(f"{MBTA_BASE_URL}/schedules").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, NoServiceResult)


async def test_predictions_present_skip_schedule_lookup(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=payload(schedule(departure=iso(5))))
    )
    schedules = respx_mock.get(f"{MBTA_BASE_URL}/schedules").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, PredictionResult)
    assert not schedules.called  # no needless fallback when real-time data exists


async def test_stale_predictions_fall_back_to_schedule(respx_mock):
    # /predictions returns only already-departed times -> treated as empty.
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=payload(schedule(departure=iso(-10))))
    )
    schedules = respx_mock.get(f"{MBTA_BASE_URL}/schedules").mock(
        return_value=httpx.Response(200, json=payload(schedule(departure=iso(25))))
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, ScheduleResult)
    assert result.next_time == NOW + timedelta(minutes=25)
    assert schedules.called
