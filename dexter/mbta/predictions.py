"""Real-time predictions + the departures orchestration (PRD §5.4 → §5.5).

``DeparturesService.get_departures`` is the skill's entry point for a resolved
target: try real-time predictions; if none, fall back to today's schedule; if
that's empty too, report no service. The result is always a structured dataclass
— the agent layer formats it to speakable text (the library never invents or
formats times).

Predictions are real-time and must NEVER be cached.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ._timeutils import parse_mbta_time
from .client import MBTAClient
from .models import NoServiceResult, PredictionResult, ResolvedTarget, ScheduleResult
from .schedules import SchedulesService

# Next "2–3" departures (PRD §5.4).
MAX_DEPARTURES = 3
# A prediction this far past its time is treated as already departed.
PAST_GRACE_SECONDS = 45

# We derive the destination from the route's direction_destinations, so we don't
# need include=trip / headsigns — keep the payload minimal with sparse fields.
_PREDICTION_FIELDS = "arrival_time,departure_time"


class DeparturesService:
    """Predictions with schedule fallback for a resolved target."""

    def __init__(self, client: MBTAClient, schedules: SchedulesService | None = None) -> None:
        self._client = client
        self._schedules = schedules or SchedulesService(client)

    async def get_departures(
        self, target: ResolvedTarget, *, now: datetime | None = None
    ) -> PredictionResult | ScheduleResult | NoServiceResult:
        now = now or datetime.now(UTC)

        prediction = await self._fetch_predictions(target, now)
        if prediction is not None:
            return prediction

        scheduled = await self._schedules.next_scheduled(target, now=now)
        if scheduled is not None:
            return scheduled

        return NoServiceResult(target=target)

    async def _fetch_predictions(
        self, target: ResolvedTarget, now: datetime
    ) -> PredictionResult | None:
        # A filter is mandatory or /predictions returns nothing (PRD §5.4).
        data = await self._client.get_json(
            "/predictions",
            params={
                "filter[stop]": ",".join(target.stop_ids),
                "filter[route]": target.route_id,
                "filter[direction_id]": target.direction_id,
                "sort": "departure_time",
                "fields[prediction]": _PREDICTION_FIELDS,
            },
        )
        minutes = _relative_minutes(data.get("data", []), now)
        if not minutes:
            return None
        return PredictionResult(target=target, minutes_away=tuple(minutes[:MAX_DEPARTURES]))


def _departure_instant(item: dict) -> datetime | None:
    attrs = item.get("attributes", {})
    # Terminal stops have no departure_time; fall back to arrival_time (PRD §11).
    return parse_mbta_time(attrs.get("departure_time")) or parse_mbta_time(
        attrs.get("arrival_time")
    )


def _relative_minutes(items: list[dict], now: datetime) -> list[int]:
    """Upcoming departures as floored relative minutes, ascending.

    Already-departed times are dropped; sub-minute times become 0 (the formatter
    renders that as "now"/"arriving").
    """
    upcoming: list[tuple[datetime, int]] = []
    for item in items:
        when = _departure_instant(item)
        if when is None:
            continue
        delta = (when - now).total_seconds()
        if delta < -PAST_GRACE_SECONDS:
            continue
        upcoming.append((when, max(0, int(delta // 60))))

    upcoming.sort(key=lambda pair: pair[0])
    return [minutes for _, minutes in upcoming]
