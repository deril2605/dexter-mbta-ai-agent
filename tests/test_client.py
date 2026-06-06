"""Milestone 1 — MBTA HTTP client: auth, caching, and typed error mapping.

All HTTP is respx-mocked; no live API calls.
"""

from __future__ import annotations

import httpx
import pytest

from dexter.mbta.client import (
    MBTAClient,
    MBTAError,
    MBTARateLimitError,
    MBTAUnavailableError,
)

from .conftest import MBTA_BASE_URL, FakeClock


async def test_get_json_returns_parsed_body(respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "116"}]})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        data = await client.get_json("/routes")

    assert data == {"data": [{"id": "116"}]}
    assert route.called


async def test_api_key_sent_as_header_when_present(respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL, api_key="secret-key") as client:
        await client.get_json("/routes")

    assert route.calls.last.request.headers["X-API-Key"] == "secret-key"


async def test_no_api_key_header_when_absent(respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        await client.get_json("/routes")

    assert "X-API-Key" not in route.calls.last.request.headers


async def test_cache_ttl_honored(respx_mock):
    clock = FakeClock(1000.0)
    route = respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL, time_func=clock) as client:
        await client.get_json("/routes", cache_ttl=100)
        await client.get_json("/routes", cache_ttl=100)  # within TTL -> cached
        assert route.call_count == 1

        clock.advance(101)  # past TTL
        await client.get_json("/routes", cache_ttl=100)
        assert route.call_count == 2  # refetched


async def test_realtime_calls_are_never_cached(respx_mock):
    # Predictions/schedules pass no cache_ttl -> every call hits the network.
    route = respx_mock.get(f"{MBTA_BASE_URL}/predictions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        await client.get_json("/predictions", params={"filter[stop]": "1"})
        await client.get_json("/predictions", params={"filter[stop]": "1"})

    assert route.call_count == 2


async def test_429_raises_rate_limit_error(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(return_value=httpx.Response(429))
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        with pytest.raises(MBTARateLimitError):
            await client.get_json("/routes")


async def test_5xx_raises_unavailable_error(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(return_value=httpx.Response(503))
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        with pytest.raises(MBTAUnavailableError):
            await client.get_json("/routes")


async def test_timeout_raises_unavailable_error(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(side_effect=httpx.TimeoutException("timed out"))
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        with pytest.raises(MBTAUnavailableError):
            await client.get_json("/routes")


async def test_connect_error_raises_unavailable_error(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(side_effect=httpx.ConnectError("boom"))
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        with pytest.raises(MBTAUnavailableError):
            await client.get_json("/routes")


async def test_other_4xx_raises_base_error(respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(return_value=httpx.Response(403))
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        with pytest.raises(MBTAError):
            await client.get_json("/routes")
