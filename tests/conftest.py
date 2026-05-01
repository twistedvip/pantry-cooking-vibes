"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from pantry_cooking_vibes.db import connect, init_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh SQLite DB with schema applied and canonical seed loaded."""
    db = tmp_path / "app.db"
    init_db(db_path=db)
    return db


@pytest.fixture
def seeded_db_path(db_path: Path) -> Path:
    """db_path with two recipes (one fully mapped, one with an unmapped ingredient)
    plus a single pantry item (broccoli)."""
    with connect(db_path) as conn:
        broccoli_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name = 'broccoli'"
        ).fetchone()["id"]
        other_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name != 'broccoli' ORDER BY id LIMIT 1"
        ).fetchone()["id"]

        r1 = conn.execute(
            "INSERT INTO recipes (source, source_id, name, cooking_time_min, "
            "                     servings, instructions_md, rating, rating_count) "
            "VALUES ('manual', 'r1', 'Broccoli Stir Fry', 25, 4, "
            "        'Stir fry broccoli in a hot pan.', 4.5, 100) RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, ?, ?)",
            (r1, broccoli_id, "2 cups broccoli florets"),
        )
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, ?, ?)",
            (r1, other_id, "1 lb other ingredient"),
        )
        for tag in ("quick", "asian"):
            conn.execute("INSERT INTO recipe_tags VALUES (?, ?)", (r1, tag))

        r2 = conn.execute(
            "INSERT INTO recipes (source, source_id, name, cooking_time_min, "
            "                     servings, instructions_md, rating) "
            "VALUES ('url', 'https://example.com/soup', 'Broccoli Soup', 60, 6, "
            "        'Simmer broccoli in stock.', 3.0) RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, ?, ?)",
            (r2, broccoli_id, "1 head broccoli"),
        )
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, NULL, ?)",
            (r2, "4 cups vegetable stock"),
        )
        conn.execute("INSERT INTO recipe_tags VALUES (?, ?)", (r2, "soup"))

        conn.execute(
            "INSERT INTO pantry (canonical_id, quantity, unit) VALUES (?, ?, ?)",
            (broccoli_id, 2.0, "head"),
        )
    return db_path
