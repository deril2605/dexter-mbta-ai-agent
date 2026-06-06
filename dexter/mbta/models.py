"""Typed dataclasses returned by the MBTA core library.

These are structured, speakable-ready domain objects — never strings. Formatting
to spoken text happens in the agent layer so the same data can later feed a voice
client. More result types (predictions, schedules, disambiguation) are added in
later milestones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# MBTA GTFS route types (the `type` attribute on a route).
ROUTE_TYPE_LIGHT_RAIL = 0
ROUTE_TYPE_HEAVY_RAIL = 1
ROUTE_TYPE_COMMUTER_RAIL = 2
ROUTE_TYPE_BUS = 3
ROUTE_TYPE_FERRY = 4


@dataclass(frozen=True, slots=True)
class Route:
    """A single MBTA route, as needed for resolution and direction lookup.

    Direction is always resolved from ``direction_destinations`` (route-specific!)
    — never from a hardcoded ``direction_id`` 0/1.
    """

    id: str
    short_name: str
    long_name: str
    type: int | None
    direction_names: tuple[str, ...]
    direction_destinations: tuple[str, ...]

    @property
    def is_bus(self) -> bool:
        return self.type == ROUTE_TYPE_BUS

    @property
    def display_name(self) -> str:
        """A speakable name for the route (e.g. ``"116"`` or ``"Blue Line"``)."""
        return self.short_name or self.long_name

    @classmethod
    def from_jsonapi(cls, resource: dict[str, Any]) -> Route:
        """Build a :class:`Route` from a JSON:API ``route`` resource object."""
        attrs = resource.get("attributes", {})
        return cls(
            id=resource["id"],
            short_name=attrs.get("short_name") or "",
            long_name=attrs.get("long_name") or "",
            type=attrs.get("type"),
            direction_names=tuple(attrs.get("direction_names") or ()),
            direction_destinations=tuple(attrs.get("direction_destinations") or ()),
        )


@dataclass(frozen=True, slots=True)
class Stop:
    """A single MBTA stop on a route (only what resolution needs)."""

    id: str
    name: str

    @classmethod
    def from_jsonapi(cls, resource: dict[str, Any]) -> Stop:
        attrs = resource.get("attributes", {})
        return cls(id=resource["id"], name=attrs.get("name") or "")


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A fully resolved (route, stop, direction) — the input to predictions.

    Carries speakable labels (``route_name``, ``stop_name``,
    ``direction_destination``) so downstream results and the formatter never need
    to re-derive them from raw IDs.
    """

    route_id: str
    route_name: str
    stop_ids: tuple[str, ...]  # one human name can map to several platform stop_ids
    stop_name: str
    direction_id: int
    direction_destination: str
    route_type: int | None = None  # GTFS route type, for "bus" vs "train" phrasing


class DisambiguationKind(StrEnum):
    """What kind of clarification is being asked for."""

    ROUTE = "route"
    STOP = "stop"
    DIRECTION = "direction"


@dataclass(frozen=True, slots=True)
class DisambiguationOption:
    """One choice offered to the user, carrying the payload to resolve it.

    ``label`` is human-readable data straight from the API (a stop name, a
    destination, a route name) — the agent's formatter composes the actual
    question sentence; the library never returns prose.
    """

    label: str
    route_id: str | None = None
    stop_ids: tuple[str, ...] = ()
    direction_id: int | None = None


@dataclass(frozen=True, slots=True)
class Disambiguation:
    """A request for clarification, with structured options and carried context.

    The agent stores this and resolves the user's next turn against ``options``.
    Already-known facts (``route_id``/``stop_id``) are carried so the follow-up
    only needs to fill the missing slot.
    """

    kind: DisambiguationKind
    options: tuple[DisambiguationOption, ...] = ()
    route_id: str | None = None
    stop_ids: tuple[str, ...] = ()  # already-resolved stop (for a DIRECTION question)
    stop_name: str | None = None
    query: str | None = None


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """Real-time predictions: the next departures as relative minutes from now."""

    target: ResolvedTarget
    minutes_away: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """Schedule fallback: the next scheduled departure (tz-aware datetime)."""

    target: ResolvedTarget
    next_time: datetime


@dataclass(frozen=True, slots=True)
class NoServiceResult:
    """No remaining real-time or scheduled service for this target today."""

    target: ResolvedTarget
