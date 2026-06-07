"""Parent-station lookup for the facilities skill.

"Is the elevator at Park Street working?" names a *station* with no route, so
route-first stop resolution doesn't apply. We instead load the bounded set of
parent stations (``/stops?filter[location_type]=1`` — a few hundred) once, cache
it 24h, and fuzzy-match a name within that set, reusing the same stop matcher as
route resolution. This is a narrower, deliberate exception to "never fuzzy-match
across all stops": stations only, not the full stop universe.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .client import MBTAClient
from .models import Stop
from .resolution import _group_by_name, _match_stop, _StopGroup

# location_type=1 selects parent stations.
_STATION_LOCATION_TYPE = 1
_STATION_FIELDS = "name"
DEFAULT_STATIONS_TTL = 24 * 60 * 60


class StationCache:
    """In-memory, fuzzy-matchable cache of MBTA parent stations."""

    def __init__(
        self,
        client: MBTAClient,
        *,
        ttl: float = DEFAULT_STATIONS_TTL,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._time = time_func
        self._groups: list[_StopGroup] = []
        self._loaded_at: float | None = None

    async def refresh(self) -> None:
        data = await self._client.get_json(
            "/stops",
            params={
                "filter[location_type]": _STATION_LOCATION_TYPE,
                "fields[stop]": _STATION_FIELDS,
            },
            cache_ttl=self._ttl,
        )
        stops = [Stop.from_jsonapi(r) for r in data.get("data", [])]
        self._groups = _group_by_name(stops)
        self._loaded_at = self._time()

    async def _ensure_fresh(self) -> None:
        if self._loaded_at is None or (self._time() - self._loaded_at) >= self._ttl:
            await self.refresh()

    async def lookup(self, name: str | None) -> _StopGroup | None:
        """Best-matching station for a name, or None when there's no clear match."""
        if not name or not name.strip():
            return None
        await self._ensure_fresh()
        return _match_stop(name, self._groups).winner
