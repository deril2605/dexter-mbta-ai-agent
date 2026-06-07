"""Observability — MBTA spans carry timing/status but never secrets.

Uses OpenTelemetry's in-memory exporter; no Phoenix needed. (Tracing-disabled
behaviour is covered by every other client test running with the no-op tracer.)
"""

from __future__ import annotations

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from dexter.mbta.client import MBTAClient
from dexter.observability import configure_tracing

from .conftest import MBTA_BASE_URL


@pytest.fixture(scope="module")
def _exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)  # only place we set the global provider
    return exporter


@pytest.fixture
def spans(_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    _exporter.clear()
    return _exporter


def _mbta_spans(exporter: InMemorySpanExporter):
    return [s for s in exporter.get_finished_spans() if s.name == "mbta.get"]


async def test_get_json_span_has_safe_attributes_only(spans, respx_mock):
    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL, api_key="super-secret-key") as client:
        await client.get_json("/routes")

    mbta = _mbta_spans(spans)
    assert len(mbta) == 1
    span = mbta[0]
    # Exactly the safe attributes — nothing else (no headers, no api key).
    assert set(span.attributes.keys()) == {"http.route", "http.status_code", "cache_hit"}
    assert span.attributes["http.route"] == "/routes"
    assert span.attributes["http.status_code"] == 200
    assert span.attributes["cache_hit"] is False
    assert "super-secret-key" not in repr(dict(span.attributes))


async def test_cache_hit_is_recorded_on_span(spans, respx_mock):
    route = respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        await client.get_json("/routes", cache_ttl=100)  # miss -> network
        await client.get_json("/routes", cache_ttl=100)  # hit -> cache

    mbta = _mbta_spans(spans)
    assert len(mbta) == 2
    assert route.call_count == 1  # second served from cache
    assert sorted(s.attributes["cache_hit"] for s in mbta) == [False, True]


async def test_error_status_recorded_on_span(spans, respx_mock):
    from dexter.mbta.client import MBTARateLimitError

    respx_mock.get(f"{MBTA_BASE_URL}/routes").mock(return_value=httpx.Response(429))
    async with MBTAClient(base_url=MBTA_BASE_URL) as client:
        with pytest.raises(MBTARateLimitError):
            await client.get_json("/routes")

    span = _mbta_spans(spans)[0]
    assert span.attributes["http.status_code"] == 429


def test_configure_tracing_is_noop_when_disabled():
    class _Settings:
        dexter_tracing = False
        dexter_tracing_endpoint = None

    assert configure_tracing(_Settings()) is False
