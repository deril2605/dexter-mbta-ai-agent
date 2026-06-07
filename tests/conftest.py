"""Shared test fixtures and sample MBTA JSON:API payloads."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dexter.mbta.models import ResolvedTarget

# A fixed "now" for deterministic time math: 2026-06-06 15:42 UTC == 11:42 EDT.
NOW = datetime(2026, 6, 6, 15, 42, 0, tzinfo=UTC)

# A fully-resolved target used by predictions/schedules tests.
TARGET = ResolvedTarget(
    route_id="116",
    route_name="116",
    stop_ids=("5740",),
    stop_name="Bennington St @ Brooks St",
    direction_id=1,
    direction_destination="Maverick",
    route_type=3,  # bus
)

# A small, realistic /routes payload covering a bus (116/117) and a subway
# line (Blue), with route-specific direction_destinations.
ROUTES_PAYLOAD = {
    "data": [
        {
            "type": "route",
            "id": "116",
            "attributes": {
                "short_name": "116",
                "long_name": "Wonderland - Maverick Station via Revere St & Bennington St",
                "type": 3,
                "direction_names": ["Outbound", "Inbound"],
                "direction_destinations": ["Wonderland", "Maverick"],
            },
        },
        {
            "type": "route",
            "id": "117",
            "attributes": {
                "short_name": "117",
                "long_name": "Wonderland - Maverick Station via Beach St & Broadway",
                "type": 3,
                "direction_names": ["Outbound", "Inbound"],
                "direction_destinations": ["Wonderland", "Maverick"],
            },
        },
        {
            "type": "route",
            "id": "Blue",
            "attributes": {
                "short_name": "",
                "long_name": "Blue Line",
                "type": 1,
                "direction_names": ["West", "East"],
                "direction_destinations": ["Bowdoin", "Wonderland"],
            },
        },
        # The Green Line is four branch routes — a generic "Green Line" token must be
        # resolved across all of them, with the named stop selecting the branch.
        *(
            {
                "type": "route",
                "id": f"Green-{letter}",
                "attributes": {
                    "short_name": letter,
                    "long_name": f"Green Line {letter}",
                    "type": 0,
                    "direction_names": ["West", "East"],
                    "direction_destinations": list(dests),
                },
            }
            for letter, dests in (
                ("B", ("Boston College", "Government Center")),
                ("C", ("Cleveland Circle", "Government Center")),
                ("D", ("Riverside", "Union Square")),
                ("E", ("Heath Street", "Medford/Tufts")),
            )
        ),
    ]
}

MBTA_BASE_URL = "https://api-v3.mbta.com"


class FakeClock:
    """A controllable monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(1000.0)


@pytest.fixture
def routes_payload() -> dict:
    return ROUTES_PAYLOAD
