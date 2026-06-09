"""SQLite-backed store for saved commutes.

Uses the stdlib ``sqlite3`` driver (no new dependency) offloaded to a worker
thread via ``asyncio.to_thread``, so the public API is ``async`` and never blocks
the event loop — consistent with the async-throughout convention. Commute volume
per user is tiny, so a fresh connection per call is simpler than pooling and has
no meaningful cost. Rows are keyed by ``(user_id, name)``; saving the same name
again upserts.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .models import SavedCommute

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS saved_commute (
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    route_id TEXT NOT NULL,
    route_name TEXT NOT NULL,
    stop_ids TEXT NOT NULL,
    stop_name TEXT NOT NULL,
    direction_id INTEGER NOT NULL,
    direction_destination TEXT NOT NULL,
    route_type INTEGER,
    walk_minutes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
)
"""

_UPSERT = """
INSERT INTO saved_commute (
    user_id, name, route_id, route_name, stop_ids, stop_name,
    direction_id, direction_destination, route_type, walk_minutes, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id, name) DO UPDATE SET
    route_id = excluded.route_id,
    route_name = excluded.route_name,
    stop_ids = excluded.stop_ids,
    stop_name = excluded.stop_name,
    direction_id = excluded.direction_id,
    direction_destination = excluded.direction_destination,
    route_type = excluded.route_type,
    walk_minutes = excluded.walk_minutes,
    created_at = excluded.created_at
"""

_COLUMNS = (
    "user_id, name, route_id, route_name, stop_ids, stop_name, "
    "direction_id, direction_destination, route_type, walk_minutes, created_at"
)


class CommuteStore:
    """Async CRUD for saved commutes over a SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def init(self) -> None:
        """Create the table if needed. Call once on startup."""
        await asyncio.to_thread(self._init_sync)

    async def save(self, commute: SavedCommute) -> SavedCommute:
        """Insert or replace a commute; returns the stored row (with ``created_at``)."""
        stored = (
            commute
            if commute.created_at
            else _replace_created_at(commute, datetime.now(UTC).isoformat())
        )
        await asyncio.to_thread(self._save_sync, stored)
        return stored

    async def get(self, user_id: str, name: str) -> SavedCommute | None:
        return await asyncio.to_thread(self._get_sync, user_id, name)

    async def list(self, user_id: str) -> tuple[SavedCommute, ...]:
        return await asyncio.to_thread(self._list_sync, user_id)

    async def delete(self, user_id: str, name: str) -> bool:
        """Delete a commute; returns True if a row was removed."""
        return await asyncio.to_thread(self._delete_sync, user_id, name)

    # --- sync bodies (run in a worker thread) -------------------------------

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread default is fine: each call opens/closes within one thread.
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sync(self) -> None:
        # Ensure the parent dir exists for a file path (skip for ":memory:").
        parent = Path(self._db_path).parent
        if self._db_path != ":memory:" and str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(_CREATE_TABLE)
            conn.commit()

    def _save_sync(self, c: SavedCommute) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                _UPSERT,
                (
                    c.user_id,
                    c.name,
                    c.route_id,
                    c.route_name,
                    json.dumps(list(c.stop_ids)),
                    c.stop_name,
                    c.direction_id,
                    c.direction_destination,
                    c.route_type,
                    c.walk_minutes,
                    c.created_at,
                ),
            )
            conn.commit()

    def _get_sync(self, user_id: str, name: str) -> SavedCommute | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM saved_commute WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
        return _row_to_commute(row) if row else None

    def _list_sync(self, user_id: str) -> tuple[SavedCommute, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM saved_commute WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return tuple(_row_to_commute(r) for r in rows)

    def _delete_sync(self, user_id: str, name: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "DELETE FROM saved_commute WHERE user_id = ? AND name = ?", (user_id, name)
            )
            conn.commit()
            return cur.rowcount > 0


def _row_to_commute(row: sqlite3.Row) -> SavedCommute:
    return SavedCommute(
        user_id=row["user_id"],
        name=row["name"],
        route_id=row["route_id"],
        route_name=row["route_name"],
        stop_ids=tuple(json.loads(row["stop_ids"])),
        stop_name=row["stop_name"],
        direction_id=row["direction_id"],
        direction_destination=row["direction_destination"],
        route_type=row["route_type"],
        walk_minutes=row["walk_minutes"],
        created_at=row["created_at"],
    )


def _replace_created_at(commute: SavedCommute, created_at: str) -> SavedCommute:
    from dataclasses import replace

    return replace(commute, created_at=created_at)
