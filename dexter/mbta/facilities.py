"""Facilities skill — elevator/escalator outages (PRD §1.5).

LLM-free. A facility outage *is* an MBTA alert with a closure ``effect``. The
default ``/alerts`` activity filter hides accessibility alerts, so we request
``filter[activity]=ALL`` and keep only elevator/escalator closures. Reuses the
alert parsing and active-window logic from :mod:`dexter.mbta.alerts`. Never cached.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .alerts import _ALERT_FIELDS, _is_active
from .client import MBTAClient
from .models import Alert

_OUTAGE_EFFECTS = frozenset({"ELEVATOR_CLOSURE", "ESCALATOR_CLOSURE"})


class FacilitiesService:
    """Elevator/escalator outages for a station or a route."""

    def __init__(self, client: MBTAClient) -> None:
        self._client = client

    async def get_outages(
        self,
        *,
        route_id: str | None = None,
        stop_ids: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> tuple[Alert, ...]:
        # filter[activity]=ALL is required, or accessibility alerts are excluded.
        params = {"filter[activity]": "ALL", "fields[alert]": _ALERT_FIELDS}
        if route_id:
            params["filter[route]"] = route_id
        if stop_ids:
            params["filter[stop]"] = ",".join(stop_ids)
        data = await self._client.get_json("/alerts", params=params)  # never cached

        now = now or datetime.now(UTC)
        outages: list[Alert] = []
        for resource in data.get("data", []):
            if not _is_active(resource, now):
                continue
            alert = Alert.from_jsonapi(resource)
            if alert.effect in _OUTAGE_EFFECTS:
                outages.append(alert)
        outages.sort(key=lambda a: a.severity, reverse=True)
        return tuple(outages)
