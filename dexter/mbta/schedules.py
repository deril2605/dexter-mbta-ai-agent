"""Schedule fallback (PRD §5.5).

Used only when real-time predictions are empty: fetch today's schedule for the
resolved target and return the next departure after now. Returns None when no
scheduled service remains today (the caller turns that into a NoServiceResult).

Schedules are real-time-adjacent and must NEVER be cached.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ._timeutils import parse_mbta_time, service_date
from .client import MBTAClient
from .models import ResolvedTarget, ScheduleResult

_SCHEDULE_FIELDS = "arrival_time,departure_time"


class SchedulesService:
    def __init__(self, client: MBTAClient) -> None:
        self._client = client

    async def next_scheduled(
        self, target: ResolvedTarget, *, now: datetime | None = None
    ) -> ScheduleResult | None:
        """Return the next scheduled departure after ``now``, or None."""
        now = now or datetime.now(UTC)
        data = await self._client.get_json(
            "/schedules",
            params={
                "filter[stop]": ",".join(target.stop_ids),
                "filter[route]": target.route_id,
                "filter[direction_id]": target.direction_id,
                "filter[date]": service_date(now),
                "sort": "departure_time",
                "fields[schedule]": _SCHEDULE_FIELDS,
            },
        )

        upcoming: list[datetime] = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            # Terminal stops have no departure_time; fall back to arrival_time.
            when = parse_mbta_time(attrs.get("departure_time")) or parse_mbta_time(
                attrs.get("arrival_time")
            )
            if when is not None and (when - now).total_seconds() >= 0:
                upcoming.append(when)

        if not upcoming:
            return None
        return ScheduleResult(target=target, next_time=min(upcoming))
