"""Saved-commute persistence (SQLite store over a temp file)."""

from __future__ import annotations

from dexter.profiles import CommuteStore, SavedCommute


def make_commute(user_id="u1", name="morning", **overrides) -> SavedCommute:
    base = dict(
        user_id=user_id,
        name=name,
        route_id="116",
        route_name="116",
        stop_ids=("5740", "5741"),
        stop_name="Bennington St @ Brooks St",
        direction_id=1,
        direction_destination="Maverick",
        route_type=3,
        walk_minutes=5,
    )
    base.update(overrides)
    return SavedCommute(**base)


async def store_at(tmp_path) -> CommuteStore:
    store = CommuteStore(tmp_path / "dexter.db")
    await store.init()
    return store


async def test_save_then_get_roundtrips(tmp_path):
    store = await store_at(tmp_path)
    saved = await store.save(make_commute())
    assert saved.created_at  # stamped on save

    got = await store.get("u1", "morning")
    assert got is not None
    assert got.route_id == "116"
    assert got.stop_ids == ("5740", "5741")  # tuple survives the JSON round-trip
    assert got.walk_minutes == 5
    assert got.direction_destination == "Maverick"


async def test_save_upserts_same_name(tmp_path):
    store = await store_at(tmp_path)
    await store.save(make_commute(walk_minutes=5))
    await store.save(make_commute(walk_minutes=8, direction_destination="Wonderland"))

    got = await store.get("u1", "morning")
    assert got.walk_minutes == 8
    assert got.direction_destination == "Wonderland"
    assert len(await store.list("u1")) == 1  # upsert, not a second row


async def test_list_is_scoped_to_user(tmp_path):
    store = await store_at(tmp_path)
    await store.save(make_commute(user_id="u1", name="morning"))
    await store.save(make_commute(user_id="u1", name="evening"))
    await store.save(make_commute(user_id="u2", name="morning"))

    u1 = await store.list("u1")
    assert {c.name for c in u1} == {"morning", "evening"}
    assert len(await store.list("u2")) == 1
    assert await store.get("u2", "evening") is None  # no cross-user leakage


async def test_delete(tmp_path):
    store = await store_at(tmp_path)
    await store.save(make_commute())
    assert await store.delete("u1", "morning") is True
    assert await store.get("u1", "morning") is None
    assert await store.delete("u1", "morning") is False  # already gone
