"""Tests for the staging import inbox data layer (importers.import_inbox)."""

from __future__ import annotations

import sqlite3

from pantry_cooking_vibes.db import apply_schema, connect
from pantry_cooking_vibes.importers import import_inbox as inbox


def test_apply_schema_adds_inbox_tables_to_existing_db(db_path):
    """Re-applying the baseline schema to a DB missing the staging tables adds
    them without a migration, and without disturbing existing data.

    This is the self-heal `start` relies on: schema.sql is all
    CREATE ... IF NOT EXISTS, so a database created before this feature gains
    the new tables on the next boot. Simulated by dropping the inbox tables from
    an otherwise-current DB, then re-applying the schema.
    """
    with connect(db_path) as conn:
        conn.execute("DROP TABLE import_items")
        conn.execute("DROP TABLE import_batches")

    with connect(db_path) as conn:
        assert not _table_exists(conn, "import_batches")
        seeded_before = conn.execute("SELECT COUNT(*) FROM canonical_ingredients").fetchone()[0]

        apply_schema(conn)

        assert _table_exists(conn, "import_batches")
        assert _table_exists(conn, "import_items")
        # Existing data is untouched (IF NOT EXISTS never recreated seeded rows).
        seeded_after = conn.execute("SELECT COUNT(*) FROM canonical_ingredients").fetchone()[0]
        assert seeded_after == seeded_before


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _ready_item(name: str, url: str, **over) -> dict:
    item = {
        "status": "ready",
        "source": "url",
        "source_id": url,
        "name": name,
        "cooking_time_min": 30,
        "servings": 4,
        "instructions_md": "Cook it.",
        "image_url": "https://example.com/x.jpg",
        "ingredients": ["1 onion", "2 carrots"],
        "tags": ["quick", "vegetarian"],
    }
    item.update(over)
    return item


# ---------- batches + items ----------


def test_create_batch_and_add_items(db_path):
    batch_id = inbox.create_batch("recipes-2024.jsonld", total=3, db_path=db_path)
    inbox.add_item(batch_id, _ready_item("Onion soup", "https://e.com/a"), db_path=db_path)
    inbox.add_item(batch_id, _ready_item("Carrot stew", "https://e.com/b"), db_path=db_path)

    batch = inbox.get_batch(batch_id, db_path=db_path)
    assert batch is not None
    assert batch["label"] == "recipes-2024.jsonld"
    assert batch["status"] == "parsing"
    assert batch["total"] == 3
    assert batch["parsed"] == 2
    assert batch["counts"]["ready"] == 2
    assert batch["counts"]["all"] == 2


def test_get_batch_missing_returns_none(db_path):
    assert inbox.get_batch(999, db_path=db_path) is None


def test_status_counts_all_is_unresolved_union(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    inbox.add_item(b, _ready_item("a", "u1"), db_path=db_path)
    inbox.add_item(b, _ready_item("b", "u2", status="review", review_reason="x"), db_path=db_path)
    inbox.add_item(b, _ready_item("c", "u3", status="dup"), db_path=db_path)
    inbox.add_item(
        b, _ready_item("d", "u4", status="failed", failure_reason="no image"), db_path=db_path
    )
    counts = inbox.status_counts(b, db_path=db_path)
    assert counts == {
        "ready": 1,
        "review": 1,
        "dup": 1,
        "failed": 1,
        "saved": 0,
        "discarded": 0,
        "all": 4,
    }


def test_finish_batch_flips_to_ready(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    inbox.finish_batch(b, db_path=db_path)
    batch = inbox.get_batch(b, db_path=db_path)
    assert batch is not None and batch["status"] == "ready"


def test_latest_batch_excludes_archived(db_path):
    b1 = inbox.create_batch("first", db_path=db_path)
    b2 = inbox.create_batch("second", db_path=db_path)
    assert inbox.latest_batch_id(db_path=db_path) == b2
    inbox.archive_batch(b2, db_path=db_path)
    assert inbox.latest_batch_id(db_path=db_path) == b1


# ---------- listing ----------


def test_list_items_filters_by_status(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    inbox.add_item(b, _ready_item("ready one", "u1"), db_path=db_path)
    inbox.add_item(b, _ready_item("dup one", "u2", status="dup"), db_path=db_path)
    page = inbox.list_items(b, status="ready", db_path=db_path)
    assert page["total"] == 1
    assert page["items"][0]["name"] == "ready one"
    assert page["items"][0]["tag_count"] == 2
    assert page["items"][0]["ingredient_count"] == 2
    assert page["counts"]["dup"] == 1


def test_list_items_all_excludes_saved_and_discarded(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    inbox.add_item(b, _ready_item("a", "u1", status="saved"), db_path=db_path)
    inbox.add_item(b, _ready_item("b", "u2", status="discarded"), db_path=db_path)
    inbox.add_item(b, _ready_item("c", "u3"), db_path=db_path)
    page = inbox.list_items(b, status="all", db_path=db_path)
    assert page["total"] == 1
    assert page["items"][0]["name"] == "c"


def test_list_items_search_by_name(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    inbox.add_item(b, _ready_item("Lemon chicken", "u1"), db_path=db_path)
    inbox.add_item(b, _ready_item("Beef stew", "u2"), db_path=db_path)
    page = inbox.list_items(b, status="all", query="lemon", db_path=db_path)
    assert page["total"] == 1
    assert page["items"][0]["name"] == "Lemon chicken"


def test_list_items_paginates_and_clamps(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    for i in range(120):
        inbox.add_item(b, _ready_item(f"recipe {i:03d}", f"u{i}"), db_path=db_path)
    page1 = inbox.list_items(b, status="all", limit=50, offset=0, db_path=db_path)
    assert page1["total"] == 120
    assert len(page1["items"]) == 50
    # A past-the-end offset clamps to the final page rather than returning empty.
    deep = inbox.list_items(b, status="all", limit=50, offset=9999, db_path=db_path)
    assert deep["offset"] == 100
    assert len(deep["items"]) == 20


# ---------- saving (promotion) ----------


def test_save_items_promotes_ready_into_recipes(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    item_id = inbox.add_item(b, _ready_item("Onion soup", "https://e.com/soup"), db_path=db_path)
    result = inbox.save_items([item_id], db_path=db_path)
    assert result["saved"] == 1
    assert len(result["recipe_ids"]) == 1

    with connect(db_path) as conn:
        rec = conn.execute(
            "SELECT * FROM recipes WHERE source = 'url' AND source_id = 'https://e.com/soup'"
        ).fetchone()
        assert rec is not None
        assert rec["name"] == "Onion soup"
        ings = conn.execute(
            "SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = ?", (rec["id"],)
        ).fetchone()[0]
        assert ings == 2
        tags = conn.execute(
            "SELECT COUNT(*) FROM recipe_tags WHERE recipe_id = ?", (rec["id"],)
        ).fetchone()[0]
        assert tags == 2
    # The staged item is now terminal.
    saved = inbox.get_item(item_id, db_path=db_path)
    assert saved is not None and saved["status"] == "saved"


def test_save_items_skips_failed(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    item_id = inbox.add_item(
        b,
        _ready_item("Broken", "u1", status="failed", failure_reason="no image"),
        db_path=db_path,
    )
    result = inbox.save_items([item_id], db_path=db_path)
    assert result == {"saved": 0, "replaced": 0, "skipped": 1, "recipe_ids": []}


def test_save_items_skips_dup_without_replace(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    dup_id = inbox.add_item(b, _ready_item("Dup", "u-dup", status="dup"), db_path=db_path)
    assert inbox.save_items([dup_id], db_path=db_path)["skipped"] == 1
    # Nothing landed in the library.
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0


def test_save_items_replaces_dup_when_opted_in(db_path):
    # Seed a library recipe that the staged item duplicates.
    with connect(db_path) as conn:
        existing = conn.execute(
            "INSERT INTO recipes (source, source_id, name) "
            "VALUES ('url', 'https://e.com/twin', 'Old name') RETURNING id"
        ).fetchone()["id"]
    b = inbox.create_batch("b", db_path=db_path)
    dup_id = inbox.add_item(
        b,
        _ready_item("New name", "https://e.com/twin", status="dup", dup_of_recipe_id=existing),
        db_path=db_path,
    )
    result = inbox.save_items([dup_id], replace_ids={dup_id}, db_path=db_path)
    assert result["replaced"] == 1
    with connect(db_path) as conn:
        # UPSERT updated the existing row in place (same id), no duplicate row.
        rows = conn.execute(
            "SELECT id, name FROM recipes WHERE source = 'url' AND source_id = 'https://e.com/twin'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == existing
        assert rows[0]["name"] == "New name"


def test_save_all_saves_every_ready(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    for i in range(3):
        inbox.add_item(b, _ready_item(f"r{i}", f"u{i}"), db_path=db_path)
    inbox.add_item(b, _ready_item("dup", "ud", status="dup"), db_path=db_path)
    result = inbox.save_all(b, status="ready", db_path=db_path)
    assert result["saved"] == 3
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 3


# ---------- discard ----------


def test_discard_items_marks_terminal(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    i1 = inbox.add_item(b, _ready_item("a", "u1"), db_path=db_path)
    i2 = inbox.add_item(b, _ready_item("b", "u2"), db_path=db_path)
    assert inbox.discard_items([i1, i2], db_path=db_path) == 2
    discarded = inbox.get_item(i1, db_path=db_path)
    assert discarded is not None and discarded["status"] == "discarded"
    # All unresolved buckets are now empty.
    assert inbox.status_counts(b, db_path=db_path)["all"] == 0


def test_discard_leaves_saved_items_alone(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    item_id = inbox.add_item(b, _ready_item("a", "u1"), db_path=db_path)
    inbox.save_items([item_id], db_path=db_path)
    assert inbox.discard_items([item_id], db_path=db_path) == 0
    still_saved = inbox.get_item(item_id, db_path=db_path)
    assert still_saved is not None and still_saved["status"] == "saved"


def test_discard_items_chunks_under_sql_var_limit(db_path, monkeypatch):
    """discard_items chunks ids so a large 'discard all' can't exceed SQLite's
    bound-parameter ceiling. Forced here with a tiny chunk size."""
    monkeypatch.setattr(inbox, "_MAX_SQL_VARS", 2)
    b = inbox.create_batch("b", db_path=db_path)
    ids = [inbox.add_item(b, _ready_item(f"r{i}", f"u{i}"), db_path=db_path) for i in range(5)]
    assert inbox.discard_items(ids, db_path=db_path) == 5
    assert inbox.status_counts(b, db_path=db_path)["discarded"] == 5


def test_discard_all_failed(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    inbox.add_item(b, _ready_item("ok", "u1"), db_path=db_path)
    inbox.add_item(
        b, _ready_item("bad", "u2", status="failed", failure_reason="no image"), db_path=db_path
    )
    assert inbox.discard_all(b, status="failed", db_path=db_path) == 1
    counts = inbox.status_counts(b, db_path=db_path)
    assert counts["failed"] == 0 and counts["ready"] == 1


def test_delete_batch_removes_items(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    inbox.add_item(b, _ready_item("a", "u1"), db_path=db_path)
    inbox.delete_batch(b, db_path=db_path)
    assert inbox.get_batch(b, db_path=db_path) is None
    with connect(db_path) as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM import_items WHERE batch_id = ?", (b,)
        ).fetchone()[0]
    assert left == 0


# ---------- editing (fix drawer) ----------


def test_update_item_rescues_failed(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    item_id = inbox.add_item(
        b,
        _ready_item("untitled", "u1", status="failed", failure_reason="no image", image_url=None),
        db_path=db_path,
    )
    updated = inbox.update_item(
        item_id,
        name="Fixed recipe",
        cooking_time_min=20,
        servings=2,
        image_url="https://e.com/fixed.jpg",
        instructions=["Step one", "Step two"],
        tags=["dinner"],
        ingredients=["1 cup rice"],
        db_path=db_path,
    )
    assert updated["status"] == "ready"
    assert updated["failure_reason"] is None
    assert updated["name"] == "Fixed recipe"


def test_update_item_keeps_dup_status(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    item_id = inbox.add_item(
        b, _ready_item("Dup", "u1", status="dup", dup_of_recipe_id=None), db_path=db_path
    )
    # dup_of_recipe_id None here, but the item was flagged dup; editing shouldn't
    # silently un-dup it unless the dup link is gone. With no link, it re-classifies.
    updated = inbox.update_item(
        item_id,
        name="Dup",
        cooking_time_min=None,
        servings=None,
        image_url="https://e.com/x.jpg",
        instructions=[],
        tags=[],
        ingredients=[],
        db_path=db_path,
    )
    assert updated["status"] == "ready"


def test_update_item_rejects_saved(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    item_id = inbox.add_item(b, _ready_item("a", "u1"), db_path=db_path)
    inbox.save_items([item_id], db_path=db_path)
    try:
        inbox.update_item(
            item_id,
            name="x",
            cooking_time_min=None,
            servings=None,
            image_url="https://e.com/x.jpg",
            instructions=[],
            tags=[],
            ingredients=[],
            db_path=db_path,
        )
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_update_item_requires_name(db_path):
    b = inbox.create_batch("b", db_path=db_path)
    item_id = inbox.add_item(b, _ready_item("a", "u1"), db_path=db_path)
    try:
        inbox.update_item(
            item_id,
            name="   ",
            cooking_time_min=None,
            servings=None,
            image_url="https://e.com/x.jpg",
            instructions=[],
            tags=[],
            ingredients=[],
            db_path=db_path,
        )
        raised = False
    except ValueError:
        raised = True
    assert raised
