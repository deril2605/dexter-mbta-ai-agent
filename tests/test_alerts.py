"""Alerts + facilities skills and the station cache (MBTA respx-mocked).

Covers: active-period filtering and severity sort; the optional stop filter; the
elevator/escalator effect filter + the required ALL activity; station-by-name
lookup scoped to parent stations.
"""

from __future__ import annotations

import httpx

from dexter.mbta.alerts import AlertsService
from dexter.mbta.client import MBTAClient
from dexter.mbta.facilities import FacilitiesService
from dexter.mbta.stations import StationCache

from .conftest import MBTA_BASE_URL, NOW

# Eastern-offset windows around the fixed NOW (2026-06-06 15:42 UTC).
_COVERS_NOW = [{"start": "2026-06-06T00:00:00-04:00", "end": "2026-06-07T00:00:00-04:00"}]
_ALREADY_ENDED = [{"start": "2026-06-01T00:00:00-04:00", "end": "2026-06-05T00:00:00-04:00"}]


def alert(effect: str, severity: int = 1, header: str = "", active_period=None) -> dict:
    attrs = {"effect": effect, "severity": severity, "header": header, "short_header": header}
    if active_period is not None:
        attrs["active_period"] = active_period
    return {"type": "alert", "id": header or effect, "attributes": attrs}


def alerts_response(*resources: dict) -> dict:
    return {"data": list(resources)}


# --- AlertsService ----------------------------------------------------------


async def test_get_alerts_keeps_active_sorted_by_severity(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200,
            json=alerts_response(
                alert("DELAY", 3, "Minor delays."),  # no period -> active
                alert("SUSPENSION", 9, "Suspended.", active_period=_COVERS_NOW),
                alert("DETOUR", 5, "Old detour.", active_period=_ALREADY_ENDED),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        alerts = await AlertsService(client).get_alerts(route_id="Blue", now=NOW)

    # Expired detour dropped; remaining sorted most-severe first.
    assert [a.effect for a in alerts] == ["SUSPENSION", "DELAY"]


async def test_get_alerts_drops_informational_notices(respx_mock):
    # A high-severity NOTICE ("predictions unavailable") must not outrank a real
    # service alert — it's dropped entirely, even though its severity is higher.
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200,
            json=alerts_response(
                alert("NOTICE", 6, "Predictions temporarily unavailable."),
                alert("DELAY", 4, "Blue Line delays."),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        alerts = await AlertsService(client).get_alerts(route_id="Blue", now=NOW)

    assert [a.effect for a in alerts] == ["DELAY"]


async def test_get_alerts_notice_only_reads_as_clear(respx_mock):
    # The Green Line case: only a feed notice is active -> no service alerts.
    respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200,
            json=alerts_response(alert("NOTICE", 4, "Predictions temporarily unavailable.")),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        alerts = await AlertsService(client).get_alerts(route_id="Green-B", now=NOW)

    assert alerts == ()


async def test_get_alerts_adds_stop_filter(respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(200, json=alerts_response())
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        await AlertsService(client).get_alerts(route_id="Blue", stop_ids=("a", "b"), now=NOW)

    params = route.calls.last.request.url.params
    assert params["filter[route]"] == "Blue"
    assert params["filter[stop]"] == "a,b"


# --- FacilitiesService ------------------------------------------------------


async def test_get_outages_keeps_only_elevator_escalator(respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/alerts").mock(
        return_value=httpx.Response(
            200,
            json=alerts_response(
                alert("DELAY", 5, "Delays."),
                alert("ELEVATOR_CLOSURE", 7, "Elevator down."),
                alert("ESCALATOR_CLOSURE", 6, "Escalator down."),
            ),
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        outages = await FacilitiesService(client).get_outages(stop_ids=("place-pktrm",), now=NOW)

    assert {o.effect for o in outages} == {"ELEVATOR_CLOSURE", "ESCALATOR_CLOSURE"}
    # Accessibility alerts are hidden unless we ask for ALL activities.
    assert route.calls.last.request.url.params["filter[activity]"] == "ALL"


# --- StationCache -----------------------------------------------------------


async def test_station_cache_fuzzy_matches_name(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"type": "stop", "id": "place-pktrm", "attributes": {"name": "Park Street"}},
                    {
                        "type": "stop",
                        "id": "place-dwnxg",
                        "attributes": {"name": "Downtown Crossing"},
                    },
                ]
            },
        )
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        station = await StationCache(client).lookup("park st")

    assert station is not None
    assert station.name == "Park Street"
    assert station.ids == ("place-pktrm",)


async def test_station_cache_filters_to_parent_stations(respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/stops").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        result = await StationCache(client).lookup("anything")

    assert result is None
    assert route.calls.last.request.url.params["filter[location_type]"] == "1"
