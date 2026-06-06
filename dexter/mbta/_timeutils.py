"""Internal time helpers shared by predictions and schedules.

MBTA times are ISO-8601 with an embedded offset (e.g. ``2024-06-06T11:42:00-04:00``)
so relative-minute math is timezone-agnostic. The MBTA timezone is only needed to
compute the service date for ``filter[date]``.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

MBTA_TZ = ZoneInfo("America/New_York")


def parse_mbta_time(value: str | None) -> datetime | None:
    """Parse an MBTA ISO-8601 timestamp to an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def service_date(now: datetime) -> str:
    """The MBTA service date (``YYYY-MM-DD``) for ``now``, in Eastern time."""
    return now.astimezone(MBTA_TZ).date().isoformat()
