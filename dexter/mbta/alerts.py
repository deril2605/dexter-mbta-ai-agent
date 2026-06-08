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
from .models import Alert, LineStatus

_ALERT_FIELDS = "header,short_header,effect,severity,lifecycle,active_period"
# System status also needs informed_entity to know which line each alert hits.
_SYSTEM_ALERT_FIELDS = _ALERT_FIELDS + ",informed_entity"

# GTFS route types for the rapid-transit network shown in a system summary.
_RAPID_TRANSIT_ROUTE_TYPES = (0, 1)  # light rail (Green/Mattapan) + heavy rail (Red/Orange/Blue)

# route id -> speakable line label, for grouping system-wide alerts by line.
_LINE_LABELS = {
    "Red": "Red Line",
    "Orange": "Orange Line",
    "Blue": "Blue Line",
    "Mattapan": "Mattapan Line",
}

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

    async def get_system_alerts(
        self,
        *,
        route_types: tuple[int, ...] = _RAPID_TRANSIT_ROUTE_TYPES,
        now: datetime | None = None,
    ) -> tuple[LineStatus, ...]:
        """Active disruptions across the rapid-transit network, grouped by line.

        One ``/alerts`` call filtered by route type; only lines with an active
        disruption are returned, worst-first. Real-time, never cached.
        """
        params = {
            "filter[route_type]": ",".join(str(t) for t in route_types),
            "fields[alert]": _SYSTEM_ALERT_FIELDS,
        }
        data = await self._client.get_json("/alerts", params=params)  # never cached
        alerts = _active_alerts(data.get("data", []), now or datetime.now(UTC))
        return _group_by_line(alerts)


def _line_label(route_id: str) -> str | None:
    """Map a route id to a speakable line label, or None if it isn't a tracked line."""
    if route_id in _LINE_LABELS:
        return _LINE_LABELS[route_id]
    if route_id.startswith("Green-"):
        return "Green Line"  # collapse the four branches into one line
    if route_id.startswith("CR-"):
        return "Commuter Rail"
    return None


def _group_by_line(alerts: tuple[Alert, ...]) -> tuple[LineStatus, ...]:
    """Keep the worst active alert per line. ``alerts`` is already severity-desc, so
    the first alert seen for a line is its worst; line order reflects worst-first."""
    worst: dict[str, Alert] = {}
    order: list[str] = []
    for alert in alerts:
        for route_id in alert.routes:
            label = _line_label(route_id)
            if label is None or label in worst:
                continue
            worst[label] = alert
            order.append(label)
    return tuple(LineStatus(label=label, alert=worst[label]) for label in order)


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
