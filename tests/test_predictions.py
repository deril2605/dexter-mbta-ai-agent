"""Milestone 3 — real-time predictions parsing and relative-minute conversion.

All HTTP is respx-mocked.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from dexter.mbta.client import MBTAClient
from dexter.mbta.models import PredictionResult
from dexter.mbta.predictions import DeparturesService

from .conftest import MBTA_BASE_URL, NOW, TARGET


def iso(minutes: float) -> str:
    """ISO-8601 timestamp at NOW + ``minutes`` (UTC offset)."""
    return (NOW + timedelta(minutes=minutes)).isoformat()


def prediction(departure: str | None = None, arrival: str | None = None, _id: str = "p") -> dict:
    return {
        "type": "prediction",
        "id": _id,
        "attributes": {"departure_time": departure, "arrival_time": arrival},
    }


def payload(*items: dict) -> dict:
    return {"data": list(items)}


async def test_populated_predictions_return_relative_minutes(respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(
            200,
            json=payload(
                prediction(departure=iso(4), _id="1"),
                prediction(departure=iso(12), _id="2"),
                prediction(departure=iso(19), _id="3"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, PredictionResult)
    assert result.minutes_away == (4, 12, 19)
    assert result.target is TARGET

    # Mandatory filters present (PRD §5.4).
    params = route.calls.last.request.url.params
    assert params["filter[stop]"] == "5740"
    assert params["filter[route]"] == "116"
    assert params["filter[direction_id]"] == "1"


async def test_terminal_stop_falls_back_to_arrival_time(respx_mock):
    # No departure_time (terminal); arrival_time must be used.
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json=payload(prediction(departure=None, arrival=iso(4))))
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, PredictionResult)
    assert result.minutes_away == (4,)


async def test_past_departures_dropped_and_limited_to_three(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(
            200,
            json=payload(
                prediction(departure=iso(-5), _id="past"),  # already departed -> dropped
                prediction(departure=iso(3), _id="1"),
                prediction(departure=iso(9), _id="2"),
                prediction(departure=iso(15), _id="3"),
                prediction(departure=iso(22), _id="4"),  # 4th future -> trimmed
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, PredictionResult)
    assert result.minutes_away == (3, 9, 15)


async def test_sub_minute_departure_is_zero(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(
            200,
            json=payload(prediction(departure=iso(0.5))),  # 30 seconds away
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert isinstance(result, PredictionResult)
    assert result.minutes_away == (0,)


async def test_unsorted_predictions_are_ordered_ascending(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(
            200,
            json=payload(
                prediction(departure=iso(12), _id="b"),
                prediction(departure=iso(4), _id="a"),
                prediction(departure=iso(19), _id="c"),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await DeparturesService(client).get_departures(TARGET, now=NOW)

    assert result.minutes_away == (4, 12, 19)
