"""Route cache and route-name resolution (PRD §5.2, §5.3 step 1).

Loads ``/routes`` once and holds parsed :class:`Route` objects in memory, with a
24h refresh. This is the first step of route-first resolution: turn a route token
("116", "Blue Line") into a route, and expose its ``direction_destinations`` for
direction resolution downstream.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from rapidfuzz import fuzz, process

from .client import MBTAClient
from .models import Route

# Sparse fieldset — fetch only what resolution and direction lookup need.
_ROUTE_FIELDS = "short_name,long_name,type,direction_names,direction_destinations"

# 24h per PRD §5.7.
DEFAULT_ROUTES_TTL = 24 * 60 * 60

# Below this WRatio score a fuzzy route-name match is not "confident" — return
# None so the caller can ask a clarifying question rather than guess.
DEFAULT_FUZZY_CUTOFF = 85.0


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


class RouteCache:
    """In-memory cache of MBTA routes with name-based lookup."""

    def __init__(
        self,
        client: MBTAClient,
        *,
        ttl: float = DEFAULT_ROUTES_TTL,
        fuzzy_cutoff: float = DEFAULT_FUZZY_CUTOFF,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._fuzzy_cutoff = fuzzy_cutoff
        self._time = time_func
        self._by_id: dict[str, Route] = {}
        self._loaded_at: float | None = None

    async def refresh(self) -> None:
        """Force a reload of ``/routes`` from the API."""
        data = await self._client.get_json("/routes", params={"fields[route]": _ROUTE_FIELDS})
        routes = [Route.from_jsonapi(r) for r in data.get("data", [])]
        self._by_id = {r.id: r for r in routes}
        self._loaded_at = self._time()

    async def _ensure_fresh(self) -> None:
        if self._loaded_at is None or (self._time() - self._loaded_at) >= self._ttl:
            await self.refresh()

    async def all_routes(self) -> list[Route]:
        await self._ensure_fresh()
        return list(self._by_id.values())

    async def get(self, route_id: str) -> Route | None:
        """Return the route with this exact GTFS ``route_id``, or None."""
        await self._ensure_fresh()
        return self._by_id.get(route_id)

    async def lookup(self, token: str) -> Route | None:
        """Resolve a human route token to a :class:`Route`.

        Bus routes match on ``short_name`` ("116" → route "116"); subway/CR match
        on ``long_name``/``short_name`` ("Blue Line" → route "Blue"). Returns None
        when there is no confident match, so the caller can ask for clarification.
        """
        if not token or not token.strip():
            return None
        await self._ensure_fresh()
        return self._match(_normalize(token))

    def _match(self, norm: str) -> Route | None:
        routes = list(self._by_id.values())

        # 1. Exact short_name (buses, "116", "CT2", "SL1").
        for route in routes:
            if route.short_name and _normalize(route.short_name) == norm:
                return route

        # 2. Exact long_name (subway/CR, "Blue Line", "Providence/Stoughton Line").
        for route in routes:
            if route.long_name and _normalize(route.long_name) == norm:
                return route

        # 3. Fuzzy fallback over both names; only accept a confident match.
        candidates: list[tuple[str, Route]] = []
        for route in routes:
            for name in (route.short_name, route.long_name):
                if name:
                    candidates.append((_normalize(name), route))
        if not candidates:
            return None

        best = process.extractOne(
            norm,
            [c[0] for c in candidates],
            scorer=fuzz.WRatio,
            score_cutoff=self._fuzzy_cutoff,
        )
        if best is None:
            return None
        _, _score, index = best
        return candidates[index][1]
