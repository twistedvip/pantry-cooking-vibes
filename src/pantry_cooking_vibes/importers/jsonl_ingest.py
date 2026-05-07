"""Generic JSONL recipe ingest.

External scrapers produce JSONL conforming to the contract in
``docs/jsonl_contract.md``. This module validates each line against
``RecipeRecord`` and UPSERTs into ``recipes`` / ``recipe_tags`` /
``recipe_ingredients`` keyed on ``(source, source_id)``.

If a scraper needs site-specific cleanup (editorial-marker stripping,
unit canonicalisation, etc.), it can register a Python entry-point in
the ``pantry_cooking_vibes.importers`` group; pass ``--plugin <name>`` to
``meal-cli ingest`` and the plugin's ``post_process(records)`` runs over
the raw record dicts before validation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

from pydantic import ValidationError

from pantry_cooking_vibes.db import DB_PATH, connect
from pantry_cooking_vibes.models import (
    SOURCE_NAME_RE,
    RecipeIngredientRecord,
    RecipeRecord,
)

log = logging.getLogger(__name__)


class IngestStats(TypedDict):
    processed: int
    recipes: int
    ingredients: int
    tags: int
    skipped: int


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("skipping malformed JSONL at %s:%d (%s)", path, lineno, e)
                continue


def _load_canonical_map(conn: sqlite3.Connection, source: str) -> dict[str, int]:
    """source_key -> canonical_id from the mapping queue, scoped to ``source``.

    Only reads ``status='approved'`` rows. Proposed (un-reviewed) mappings are
    intentionally excluded so ingest never wires recipe_ingredients to a
    canonical the curator hasn't sign off on. Mirrors ``apply_text_mappings``.
    """
    rows = conn.execute(
        """
        SELECT source_key, proposed_canonical_id
        FROM ingredient_mapping_queue
        WHERE source = ?
          AND status = 'approved'
          AND proposed_canonical_id IS NOT NULL
        """,
        (source,),
    ).fetchall()
    return {r["source_key"]: r["proposed_canonical_id"] for r in rows}


def _upsert_recipe(conn: sqlite3.Connection, source: str, rec: RecipeRecord) -> int:
    nutrition_str = (
        json.dumps(rec.nutrition_json, ensure_ascii=False)
        if rec.nutrition_json is not None
        else None
    )
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
            source,
            rec.source_id,
            rec.name,
            rec.cooking_time_min,
            rec.servings,
            rec.instructions_md,
            nutrition_str,
            rec.image_url,
            rec.rating,
            rec.rating_count,
        ),
    )
    row = cur.fetchone()
    return row["id"]


def _replace_tags(conn: sqlite3.Connection, recipe_id: int, tags: list[str]) -> int:
    conn.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (recipe_id,))
    seen: set[str] = set()
    inserted = 0
    for t in tags:
        norm = t.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        conn.execute(
            "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag) VALUES (?, ?)",
            (recipe_id, norm),
        )
        inserted += 1
    return inserted


def _replace_ingredients(
    conn: sqlite3.Connection,
    recipe_id: int,
    ingredients: list[RecipeIngredientRecord],
    canonical_map: dict[str, int],
) -> int:
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    inserted = 0
    for ing in ingredients:
        # canonical_hint wins as the mapping-queue lookup key; fall back to
        # the lowercased original_text so plain JSONL still routes through
        # the curator queue.
        if ing.canonical_hint and ing.canonical_hint.strip():
            key = ing.canonical_hint.strip().lower()
        elif ing.original_text:
            key = ing.original_text.strip().lower()
        else:
            key = ""
        canonical_id = canonical_map.get(key) if key else None
        conn.execute(
            """
            INSERT INTO recipe_ingredients
                (recipe_id, canonical_id, original_text, quantity, unit, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                recipe_id,
                canonical_id,
                ing.original_text,
                ing.quantity,
                ing.unit,
                ing.notes,
            ),
        )
        inserted += 1
    return inserted


def ingest_jsonl(
    jsonl_path: Path,
    source: str,
    *,
    db_path: Path | None = None,
    plugin: str | None = None,
    batch_size: int = 200,
    quiet: bool = False,
) -> IngestStats:
    """Import recipes from a JSONL file produced by an external scraper.

    Idempotent: UPSERTs on ``(source, source_id)``; tags + ingredients are
    replaced wholesale per recipe. If ``plugin`` is provided, the importer
    is loaded via entry-point and its ``post_process(records)`` runs on
    the raw dicts before Pydantic validation.

    **Transaction semantics**: this is a best-effort batched importer.
    ``conn.commit()`` is called every ``batch_size`` recipes so progress is
    durable on long files. If a row triggers a ``ValidationError`` it is
    counted as ``skipped`` and ingest continues. Other exceptions
    (sqlite IntegrityError, plugin TypeError, etc.) propagate; rows from
    the current uncommitted batch are rolled back, but earlier committed
    batches remain. Malformed JSONL lines that fail ``json.loads`` are
    silently skipped and counted into ``skipped``.
    """
    if not jsonl_path.exists():
        raise FileNotFoundError(jsonl_path)
    if not SOURCE_NAME_RE.fullmatch(source):
        raise ValueError(
            f"invalid source name {source!r}: must be lowercase letters, digits, "
            "or hyphens, starting with a letter"
        )
    db = db_path or DB_PATH

    raw_records = list(_iter_jsonl(jsonl_path))
    if plugin is not None:
        from pantry_cooking_vibes.importers.registry import load_plugin

        importer = load_plugin(plugin)
        raw_records = list(importer.post_process(raw_records))

    stats: IngestStats = {
        "processed": 0,
        "recipes": 0,
        "ingredients": 0,
        "tags": 0,
        "skipped": 0,
    }

    with connect(db) as conn:
        canonical_map = _load_canonical_map(conn, source)
        for raw in raw_records:
            stats["processed"] += 1
            try:
                rec = RecipeRecord.model_validate(raw)
            except ValidationError:
                stats["skipped"] += 1
                continue
            if not rec.image_url or not rec.image_url.strip():
                stats["skipped"] += 1
                continue
            recipe_id = _upsert_recipe(conn, source, rec)
            stats["tags"] += _replace_tags(conn, recipe_id, rec.tags)
            stats["ingredients"] += _replace_ingredients(
                conn, recipe_id, rec.ingredients, canonical_map
            )
            stats["recipes"] += 1
            if stats["recipes"] % batch_size == 0:
                conn.commit()
                if not quiet:
                    print(f"  imported {stats['recipes']} recipes...")

    return stats
