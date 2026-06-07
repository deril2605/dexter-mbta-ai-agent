"""Service alerts skill (PRD §1.5).

LLM-free. Fetches MBTA ``/alerts`` for a route (optionally narrowed to a stop),
keeps the ones active *now*, and returns them as typed :class:`Alert` objects,
most-severe first. Alerts are real-time and must NEVER be cached. Mapping the raw
``effect``/``severity`` to plain language happens in the agent's formatter.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ._timeutils import parse_mbta_time
from .client import MBTAClient
from .models import Alert

_ALERT_FIELDS = "header,short_header,effect,severity,lifecycle,active_period"

# Informational effects that aren't service disruptions. The alerts skill drops
# them so a real disruption always leads, and a line whose only "alert" is a feed
# notice (e.g. "predictions temporarily unavailable") reads as having none.
_INFORMATIONAL_EFFECTS = frozenset({"NOTICE", "SUMMARY"})


class AlertsService:
    """Active service alerts for a resolved route/stop scope."""

    def __init__(self, client: MBTAClient) -> None:
        self._client = client

    async def get_alerts(
        self,
        *,
        route_id: str,
        stop_ids: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> tuple[Alert, ...]:
        params = {"filter[route]": route_id, "fields[alert]": _ALERT_FIELDS}
        if stop_ids:
            params["filter[stop]"] = ",".join(stop_ids)
        data = await self._client.get_json("/alerts", params=params)  # never cached
        return _active_alerts(data.get("data", []), now or datetime.now(UTC))


def _active_alerts(resources: list[dict], now: datetime) -> tuple[Alert, ...]:
    alerts: list[Alert] = []
    for resource in resources:
        if not _is_active(resource, now):
            continue
        alert = Alert.from_jsonapi(resource)
        if alert.effect in _INFORMATIONAL_EFFECTS:
            continue
        alerts.append(alert)
    alerts.sort(key=lambda a: a.severity, reverse=True)
    return tuple(alerts)


def _is_active(resource: dict, now: datetime) -> bool:
    """True if any active_period covers ``now`` (or the alert lists no periods)."""
    periods = resource.get("attributes", {}).get("active_period") or []
    if not periods:
        return True
    for period in periods:
        start = parse_mbta_time(period.get("start"))
        end = parse_mbta_time(period.get("end"))
        if (start is None or start <= now) and (end is None or now <= end):
            return True
    return False
