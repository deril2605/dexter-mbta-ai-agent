"""Milestone 1 — route cache: name lookup, direction data, and TTL refresh.

All HTTP is respx-mocked; no live API calls.
"""

from __future__ import annotations

import httpx
import pytest

from dexter.mbta.client import MBTAClient
from dexter.mbta.routes import RouteCache

from .conftest import MBTA_BASE_URL, ROUTES_PAYLOAD, FakeClock


@pytest.fixture
def mock_routes(respx_mock):
    """Mock /routes and return the respx route so tests can assert call counts."""
    return respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json=ROUTES_PAYLOAD)
    )


async def test_lookup_bus_by_short_name(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client)
        route = await cache.lookup("116")

    assert route is not None
    assert route.id == "116"


async def test_lookup_subway_by_long_name(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client)
        route = await cache.lookup("Blue Line")

    assert route is not None
    assert route.id == "Blue"


async def test_lookup_is_case_insensitive(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client)
        assert (await cache.lookup("blue line")).id == "Blue"
        assert (await cache.lookup("  116 ")).id == "116"


async def test_lookup_unknown_route_returns_none(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client)
        assert await cache.lookup("Purple Line") is None
        assert await cache.lookup("") is None


async def test_get_by_route_id(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client)
        assert (await cache.get("117")).short_name == "117"
        assert await cache.get("nonexistent") is None


async def test_direction_destinations_exposed(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client)
        route = await cache.lookup("116")

    # Route-specific direction data, used later to resolve "toward Maverick".
    assert route.direction_destinations == ("Wonderland", "Maverick")
    assert route.direction_names == ("Outbound", "Inbound")


async def test_routes_loaded_once_then_served_from_cache(mock_routes):
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client)
        await cache.lookup("116")
        await cache.lookup("Blue Line")
        await cache.get("117")

    assert mock_routes.call_count == 1  # one load served all lookups


async def test_cache_refreshes_after_ttl(mock_routes):
    clock = FakeClock(0.0)
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        cache = RouteCache(client, ttl=3600, time_func=clock)
        await cache.lookup("116")
        assert mock_routes.call_count == 1

        clock.advance(1800)  # within TTL
        await cache.lookup("116")
        assert mock_routes.call_count == 1

        clock.advance(1801)  # now past the 3600s TTL
        await cache.lookup("116")
        assert mock_routes.call_count == 2
