"""Staging inbox for batch recipe import.

A *batch* is one ingestion run (pasted URLs and/or an uploaded JSON-LD file).
Its recipes are parsed into ``import_items`` and triaged there before any of
them touch the ``recipes`` library. Saving an item *promotes* a copy into
``recipes`` through the same normalizer ``url_import`` uses; discarding marks it
terminal. Nothing here is framework-bound: every entry point takes ``db_path=``
and is shared by the FastAPI routes, the CLI, and the background parse job.

Status vocabulary (``import_items.status``):
    ready      parsed cleanly, ready to save
    review     parsed but flagged (e.g. some ingredients didn't auto-map)
    dup        (source, source_id) already exists in the library
    failed     could not be parsed (no recipe data, no image, fetch error)
    saved      promoted into recipes (terminal)
    discarded  rejected by the user (terminal)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from pantry_cooking_vibes.db import DB_PATH, connect
from pantry_cooking_vibes.importers.normalize import _build_choice_map, _load_index
from pantry_cooking_vibes.importers.url_import import (
    _enqueue_ingredients,
    _load_canonical_map,
    _replace_ingredients,
    _replace_tags,
)

# The four buckets a user triages; 'all' is their union. 'saved'/'discarded'
# are terminal and excluded from the pills (surfaced in the batch summary).
UNRESOLVED_STATUSES: tuple[str, ...] = ("ready", "review", "dup", "failed")

# Cap on bound parameters per statement. SQLite's SQLITE_MAX_VARIABLE_NUMBER is
# 999 on older builds, so an `IN (?, ?, ...)` over a large batch must be chunked
# below this or it raises "too many SQL variables".
_MAX_SQL_VARS = 900


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------


def create_batch(label: str, *, total: int = 0, db_path: Path | None = None) -> int:
    """Create a new (parsing) batch and return its id."""
    with connect(db_path or DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO import_batches (label, total) VALUES (?, ?) RETURNING id",
            (label or None, max(0, int(total))),
        )
        return cur.fetchone()["id"]


def add_item(batch_id: int, item: dict, *, db_path: Path | None = None) -> int:
    """Insert one parsed item into a batch. Returns the new item id.

    ``item`` carries the parsed recipe fields (the ``parse_recipe`` shape) plus
    a ``status`` and, where relevant, ``dup_of_recipe_id`` / ``review_reason`` /
    ``failure_reason``.
    """
    with connect(db_path or DB_PATH) as conn:
        return _insert_item(conn, batch_id, item)


def _insert_item(conn: sqlite3.Connection, batch_id: int, item: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO import_items
            (batch_id, status, source, source_id, name, cooking_time_min, servings,
             instructions_md, nutrition_json, image_url, rating, rating_count,
             ingredients_json, tags_json, dup_of_recipe_id, review_reason, failure_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            batch_id,
            item.get("status", "ready"),
            item.get("source", "url"),
            item.get("source_id"),
            item.get("name"),
            item.get("cooking_time_min"),
            item.get("servings"),
            item.get("instructions_md"),
            item.get("nutrition_json"),
            item.get("image_url"),
            item.get("rating"),
            item.get("rating_count"),
            json.dumps(item.get("ingredients") or [], ensure_ascii=False),
            json.dumps(item.get("tags") or [], ensure_ascii=False),
            item.get("dup_of_recipe_id"),
            item.get("review_reason"),
            item.get("failure_reason"),
        ),
    )
    return cur.fetchone()["id"]


def finish_batch(batch_id: int, *, db_path: Path | None = None) -> None:
    """Mark a batch's parsing done so the UI stops showing progress."""
    with connect(db_path or DB_PATH) as conn:
        conn.execute(
            "UPDATE import_batches SET status = 'ready' WHERE id = ? AND status = 'parsing'",
            (batch_id,),
        )


def archive_batch(batch_id: int, *, db_path: Path | None = None) -> None:
    """Hide a batch from the landing query without deleting it. The data-layer
    archive primitive: ``latest_batch_id`` skips archived batches. Completed
    batches are deleted (see ``delete_batch``); archiving is the non-destructive
    alternative kept available for callers that want to keep a batch around."""
    with connect(db_path or DB_PATH) as conn:
        conn.execute("UPDATE import_batches SET status = 'archived' WHERE id = ?", (batch_id,))


def status_counts(
    batch_id: int, *, conn: sqlite3.Connection | None = None, db_path: Path | None = None
) -> dict[str, int]:
    """Count items per status for a batch, plus 'all' (the unresolved union).

    Accepts an open ``conn`` so callers already inside a transaction don't
    reopen the DB; otherwise opens its own.
    """
    if conn is not None:
        return _status_counts(conn, batch_id)
    with connect(db_path or DB_PATH) as c:
        return _status_counts(c, batch_id)


def _status_counts(conn: sqlite3.Connection, batch_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM import_items WHERE batch_id = ? GROUP BY status",
        (batch_id,),
    ).fetchall()
    counts = {s: 0 for s in ("ready", "review", "dup", "failed", "saved", "discarded")}
    for r in rows:
        counts[r["status"]] = r["n"]
    counts["all"] = sum(counts[s] for s in UNRESOLVED_STATUSES)
    return counts


def get_batch(batch_id: int, *, db_path: Path | None = None) -> dict | None:
    """Return a batch row with its per-status counts, or None if missing."""
    with connect(db_path or DB_PATH) as conn:
        row = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        batch = dict(row)
        batch["counts"] = _status_counts(conn, batch_id)
        batch["parsed"] = conn.execute(
            "SELECT COUNT(*) FROM import_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        return batch


def latest_batch_id(*, db_path: Path | None = None) -> int | None:
    """The most recent non-archived batch, the inbox's default landing target."""
    with connect(db_path or DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM import_batches WHERE status != 'archived' "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None


# ---------------------------------------------------------------------------
# item listing (filtered, paginated)
# ---------------------------------------------------------------------------

MAX_PAGE_LIMIT = 250


def _apply_item_filters(where: list[str], params: list[Any], *, status: str, query: str) -> None:
    """Append the status + search filters to a batch-item query.

    Only **literal** SQL fragments are appended to ``where``; the user-supplied
    ``status`` and search term reach ``params`` (bound via ``?``) and never the
    SQL text. Keeping user values out of the query string is what makes the
    interpolation injection-safe — and keeps CodeQL's taint off the query.

    'all' (or empty) means the unresolved union; any other status filters to it.
    """
    if status == "all" or not status:
        where.append("status IN (" + ",".join("?" * len(UNRESOLVED_STATUSES)) + ")")
        params.extend(UNRESOLVED_STATUSES)
    else:
        where.append("status = ?")
        params.append(status)
    q = query.strip()
    if q:
        where.append("name LIKE ? COLLATE NOCASE")
        params.append(f"%{q}%")


def list_items(
    batch_id: int,
    *,
    status: str = "all",
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    db_path: Path | None = None,
) -> dict:
    """Return one page of a batch's items plus the total for that filter.

    Shape mirrors the recipes browse page: ``{items, total, offset, counts}``.
    ``offset`` is clamped to the last page so a stale deep link can't show an
    empty list.
    """
    limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
    where: list[str] = ["batch_id = ?"]
    args: list[Any] = [batch_id]
    _apply_item_filters(where, args, status=status, query=query)
    # where_sql is a join of literal fragments only; all user values are in args.
    where_sql = " AND ".join(where)

    with connect(db_path or DB_PATH) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM import_items WHERE {where_sql}",  # noqa: S608
            args,
        ).fetchone()[0]
        # Clamp a past-the-end offset to the final page (limit-aligned).
        if offset >= total and total > 0:
            offset = ((total - 1) // limit) * limit
        offset = max(0, offset)
        rows = conn.execute(
            f"SELECT * FROM import_items WHERE {where_sql} "  # noqa: S608
            "ORDER BY name IS NULL, name COLLATE NOCASE, id "
            "LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        items = [_row_to_item(r) for r in rows]
        counts = _status_counts(conn, batch_id)
    return {"items": items, "total": total, "offset": offset, "counts": counts}


def _row_to_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    raw_ings = item.pop("ingredients_json") or "[]"
    raw_tags = item.pop("tags_json") or "[]"
    item["ingredients"] = json.loads(raw_ings) if isinstance(raw_ings, str) else []
    item["tags"] = json.loads(raw_tags) if isinstance(raw_tags, str) else []
    item["ingredient_count"] = len(item["ingredients"])
    item["tag_count"] = len(item["tags"])
    return item


def get_item(item_id: int, *, db_path: Path | None = None) -> dict | None:
    with connect(db_path or DB_PATH) as conn:
        row = conn.execute("SELECT * FROM import_items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None


# ---------------------------------------------------------------------------
# editing a staged item (the fix drawer)
# ---------------------------------------------------------------------------


def update_item(
    item_id: int,
    *,
    name: str,
    cooking_time_min: int | None,
    servings: int | None,
    image_url: str | None,
    instructions: list[str],
    tags: list[str],
    ingredients: list[str],
    db_path: Path | None = None,
) -> dict:
    """Apply a fix to a staged item, then re-classify it.

    A fix can rescue a ``failed`` item (it now has a name and image) or clear a
    ``review`` flag. Terminal items (saved/discarded) are not editable.
    """
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    clean_tags = [t.strip().lower() for t in tags if t.strip()]
    clean_ings = [i.strip() for i in ingredients if i.strip()]
    instructions_md = "\n".join(s.rstrip() for s in instructions).strip() or None
    image = (image_url or "").strip() or None

    with connect(db_path or DB_PATH) as conn:
        row = conn.execute("SELECT * FROM import_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError(f"import item {item_id} not found")
        if row["status"] in ("saved", "discarded"):
            raise ValueError(f"item {item_id} is {row['status']} and can no longer be edited")

        new_status, review_reason, failure_reason = _classify(
            name=name, image_url=image, dup_of_recipe_id=row["dup_of_recipe_id"]
        )
        conn.execute(
            """
            UPDATE import_items SET
                name = ?, cooking_time_min = ?, servings = ?, image_url = ?,
                instructions_md = ?, tags_json = ?, ingredients_json = ?,
                status = ?, review_reason = ?, failure_reason = ?
            WHERE id = ?
            """,
            (
                name,
                cooking_time_min,
                servings,
                image,
                instructions_md,
                json.dumps(clean_tags, ensure_ascii=False),
                json.dumps(clean_ings, ensure_ascii=False),
                new_status,
                review_reason,
                failure_reason,
                item_id,
            ),
        )
        return _row_to_item(
            conn.execute("SELECT * FROM import_items WHERE id = ?", (item_id,)).fetchone()
        )


def _classify(
    *, name: str | None, image_url: str | None, dup_of_recipe_id: int | None
) -> tuple[str, str | None, str | None]:
    """Decide an item's triage status from its fields.

    Image is required to enter the library (the ingest path skips imageless
    records), so a missing image is a hard fail. A duplicate stays a duplicate
    until the user resolves it. Otherwise it's ready.
    """
    if not name or not name.strip():
        return "failed", None, "no title parsed"
    if not image_url or not image_url.strip():
        return "failed", None, "no image"
    if dup_of_recipe_id is not None:
        return "dup", None, None
    return "ready", None, None


# ---------------------------------------------------------------------------
# save (promote into the library) / discard
# ---------------------------------------------------------------------------


def save_items(
    item_ids: list[int],
    *,
    replace_ids: set[int] | None = None,
    db_path: Path | None = None,
) -> dict:
    """Promote the given items into the recipes library.

    ``ready``/``review`` items are saved. ``dup`` items are skipped unless their
    id is in ``replace_ids`` (an explicit per-row overwrite). ``failed`` and
    already-terminal items are skipped. Returns
    ``{saved, replaced, skipped, recipe_ids}``.
    """
    replace = replace_ids or set()
    saved = replaced = skipped = 0
    recipe_ids: list[int] = []
    with connect(db_path or DB_PATH) as conn:
        canonical_map = _load_canonical_map(conn)
        # Build the canonical fuzzy-match index ONCE for the whole bulk save and
        # reuse it for every item. _enqueue_ingredients would otherwise reload
        # and rebuild it per item, making "save all" O(N) full index builds.
        choices, choice_to_id = _build_choice_map(_load_index(conn))
        for item_id in item_ids:
            row = conn.execute("SELECT * FROM import_items WHERE id = ?", (item_id,)).fetchone()
            if row is None or row["status"] in ("saved", "discarded", "failed"):
                skipped += 1
                continue
            is_dup = row["status"] == "dup"
            if is_dup and item_id not in replace:
                skipped += 1
                continue
            recipe_id = _promote(conn, row, canonical_map, choices, choice_to_id)
            conn.execute(
                "UPDATE import_items SET status = 'saved', saved_recipe_id = ? WHERE id = ?",
                (recipe_id, item_id),
            )
            recipe_ids.append(recipe_id)
            if is_dup:
                replaced += 1
            else:
                saved += 1
    return {"saved": saved, "replaced": replaced, "skipped": skipped, "recipe_ids": recipe_ids}


def _matching_ids(conn: sqlite3.Connection, batch_id: int, status: str, query: str) -> list[int]:
    """Item ids in a batch matching a filter pill + search (the set a bulk
    action operates on, so it acts on exactly what the filter shows)."""
    where: list[str] = ["batch_id = ?"]
    args: list[Any] = [batch_id]
    _apply_item_filters(where, args, status=status, query=query)
    where_sql = " AND ".join(where)  # literal fragments only; user values in args
    return [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM import_items WHERE {where_sql}",  # noqa: S608
            args,
        ).fetchall()
    ]


def save_all(
    batch_id: int,
    *,
    status: str = "ready",
    query: str = "",
    db_path: Path | None = None,
) -> dict:
    """Save every item in a batch matching a filter (the bulk 'save all ready').

    Dups are never bulk-saved (they need a per-row replace), so a status='dup'
    or 'all' bulk save skips them.
    """
    with connect(db_path or DB_PATH) as conn:
        ids = _matching_ids(conn, batch_id, status, query)
    return save_items(ids, db_path=db_path)


def discard_all(
    batch_id: int,
    *,
    status: str = "failed",
    query: str = "",
    db_path: Path | None = None,
) -> int:
    """Discard every item matching a filter (the bulk 'discard all failed').
    Returns the number discarded."""
    with connect(db_path or DB_PATH) as conn:
        ids = _matching_ids(conn, batch_id, status, query)
    return discard_items(ids, db_path=db_path)


def delete_batch(batch_id: int, *, db_path: Path | None = None) -> None:
    """Remove a batch and its items entirely. Called when every item has been
    resolved, so a finished import leaves no browsable record."""
    with connect(db_path or DB_PATH) as conn:
        conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))


def discard_items(item_ids: list[int], *, db_path: Path | None = None) -> int:
    """Mark items discarded (terminal). Already-saved items are left alone.

    Returns the number actually discarded. The id list is chunked under the
    SQLite bound-parameter limit so 'discard all' on a large batch can't blow
    the ``IN (...)`` clause.
    """
    if not item_ids:
        return 0
    discarded = 0
    with connect(db_path or DB_PATH) as conn:
        for start in range(0, len(item_ids), _MAX_SQL_VARS):
            chunk = item_ids[start : start + _MAX_SQL_VARS]
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.execute(
                # S608: placeholders is a comma-joined run of ? binds, no user text.
                f"UPDATE import_items SET status = 'discarded' "  # noqa: S608
                f"WHERE id IN ({placeholders}) AND status NOT IN ('saved', 'discarded')",
                list(chunk),
            )
            discarded += cur.rowcount
    return discarded


def _promote(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    canonical_map: dict[str, int],
    choices: list[str],
    choice_to_id: dict[str, int],
) -> int:
    """Copy one staged item into recipes (+ tags, ingredients), UPSERT on
    (source, source_id). Reuses url_import's ingredient normalizer so a saved
    recipe maps to canonicals exactly as a single URL import would. The prebuilt
    ``choices`` / ``choice_to_id`` index is passed in so a bulk save reuses one
    index instead of rebuilding it per item."""
    ingredients = json.loads(row["ingredients_json"] or "[]")
    tags = json.loads(row["tags_json"] or "[]")
    canonical_map = _enqueue_ingredients(
        conn, ingredients, canonical_map, choices=choices, choice_to_id=choice_to_id
    )
    recipe_id = _upsert_recipe_row(conn, row)
    _replace_tags(conn, recipe_id, tags)
    _replace_ingredients(conn, recipe_id, ingredients, canonical_map)
    return recipe_id


def _upsert_recipe_row(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """UPSERT a recipes row from a staged item. Parametrizes ``source`` (unlike
    url_import._upsert_recipe, which hardcodes 'url') so file-sourced items keep
    their own source."""
    cur = conn.execute(
        """
        INSERT INTO recipes
            (source, source_id, name, cooking_time_min, servings,
             instructions_md, nutrition_json, image_url, rating, rating_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            name             = excluded.name,
            cooking_time_min = excluded.cooking_time_min,
            servings         = excluded.servings,
            instructions_md  = excluded.instructions_md,
            nutrition_json   = excluded.nutrition_json,
            image_url        = excluded.image_url,
            rating           = excluded.rating,
            rating_count     = excluded.rating_count
        RETURNING id
        """,
        (
            row["source"],
            row["source_id"],
            row["name"],
            row["cooking_time_min"],
            row["servings"],
            row["instructions_md"],
            row["nutrition_json"],
            row["image_url"],
            row["rating"],
            row["rating_count"],
        ),
    )
    return cur.fetchone()["id"]
