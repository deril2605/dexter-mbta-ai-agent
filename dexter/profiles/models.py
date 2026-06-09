"""The persisted shape of a saved commute (primitive fields only).

Deliberately decoupled from ``dexter.mbta.models.ResolvedTarget``: the storage
layer stays a plain data store with no domain imports, and the agent maps between
the two. A ``SavedCommute`` is essentially a named, resolved (route, stop,
direction) plus the rider's walk time to the stop — enough to answer "should I
leave now?" without re-asking anything.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SavedCommute:
    """One named commute belonging to a single (opaque) user.

    ``name`` is the rider's label ("morning", "work"). ``walk_minutes`` is how long
    it takes them to reach the boarding stop — used by the leave-now math, never by
    the LLM. ``stop_ids`` is a tuple because one human stop can be several platforms.
    """

    user_id: str
    name: str
    route_id: str
    route_name: str
    stop_ids: tuple[str, ...]
    stop_name: str
    direction_id: int
    direction_destination: str
    route_type: int | None = None
    walk_minutes: int = 0
    created_at: str = ""  # ISO-8601 UTC; set by the store on save
