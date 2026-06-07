"""Async HTTP client for the MBTA V3 API.

LLM-free. This is the in-process hot path the agent calls directly. It owns:
- the ``httpx.AsyncClient`` with auth (``X-API-Key``) and base URL,
- a JSON:API ``GET`` helper,
- error mapping to typed exceptions (429 / timeout / 5xx / other),
- an opt-in TTL cache (caller chooses a ``cache_ttl`` per call).

Caching is opt-in by design: the only cacheable resources are ``/routes`` (24h)
and per-route ``/stops`` (~6h). Predictions and schedules are real-time and must
NEVER be cached — callers simply omit ``cache_ttl`` for those.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from opentelemetry import trace

# A no-op tracer until an OTel SDK provider is registered (see dexter.observability),
# so this adds ~no overhead when tracing is off. We never attach auth headers/keys.
_tracer = trace.get_tracer("dexter.mbta")


class MBTAError(Exception):
    """Base class for MBTA core library errors."""


class MBTARateLimitError(MBTAError):
    """The MBTA feed returned HTTP 429 (rate limited)."""


class MBTAUnavailableError(MBTAError):
    """The MBTA feed could not be reached (timeout, network error, or 5xx)."""


class _TTLCache:
    """Tiny in-memory TTL cache with per-entry expiry and an injectable clock.

    A custom clock (``time_func``) keeps TTL behaviour deterministic in tests.
    """

    def __init__(self, time_func: Callable[[], float] = time.monotonic) -> None:
        self._time = time_func
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._time() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._store[key] = (self._time() + ttl, value)

    def clear(self) -> None:
        self._store.clear()


def _cache_key(path: str, params: dict[str, Any] | None) -> str:
    if not params:
        return path
    return f"{path}?{urlencode(sorted(params.items()))}"


class MBTAClient:
    """Thin async wrapper over the MBTA V3 JSON:API endpoint."""

    DEFAULT_BASE_URL = "https://api-v3.mbta.com"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        *,
        timeout: float = 10.0,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        headers = {"Accept": "application/vnd.api+json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )
        self._cache = _TTLCache(time_func=time_func)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> MBTAClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        cache_ttl: float | None = None,
    ) -> dict[str, Any]:
        """GET a JSON:API resource and return the parsed body.

        If ``cache_ttl`` is given, a successful response is cached under
        ``(path, params)`` for that many seconds. Real-time endpoints
        (predictions/schedules) pass no ``cache_ttl`` and are never cached.
        """
        key = _cache_key(path, params)
        with _tracer.start_as_current_span("mbta.get") as span:
            span.set_attribute("http.route", path)

            if cache_ttl is not None:
                cached = self._cache.get(key)
                if cached is not None:
                    span.set_attribute("cache_hit", True)
                    return cached
            span.set_attribute("cache_hit", False)

            try:
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                raise MBTAUnavailableError(f"MBTA request to {path} timed out") from exc
            except httpx.RequestError as exc:
                raise MBTAUnavailableError(f"could not reach the MBTA feed: {exc}") from exc

            status = response.status_code
            span.set_attribute("http.status_code", status)
            if status == 429:
                raise MBTARateLimitError("MBTA feed rate limit hit (HTTP 429)")
            if status >= 500:
                raise MBTAUnavailableError(f"MBTA feed returned a server error (HTTP {status})")
            if status >= 400:
                raise MBTAError(f"MBTA request to {path} failed (HTTP {status})")

            data = response.json()
            if cache_ttl is not None:
                self._cache.set(key, data, cache_ttl)
            return data
