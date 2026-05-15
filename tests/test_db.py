"""Tests for db init, schema, migrations, FTS triggers, seed loader."""

from __future__ import annotations

import pytest

from pantry_cooking_vibes.db import (
    apply_schema,
    connect,
    get_connection,
    init_db,
    run_migrations,
    seed_canonical_ingredients,
)


def test_init_db_idempotent(tmp_path):
    db = tmp_path / "app.db"
    n1 = init_db(db_path=db)
    n2 = init_db(db_path=db)
    assert n1 > 0
    assert n2 == 0  # second run inserts nothing new

    with connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM canonical_ingredients").fetchone()[0]
    assert total == n1


def test_init_db_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nested" / "app.db"
    init_db(db_path=nested)
    assert nested.exists()


def test_migration_004_purges_image_less_recipes(db_path):
    from pantry_cooking_vibes.db import _MIGRATIONS_DIR

    migration = (_MIGRATIONS_DIR / "004_drop_recipes_without_image.sql").read_text(encoding="utf-8")

    with connect(db_path) as conn:
        ok = conn.execute(
            "INSERT INTO recipes (source, source_id, name, image_url) "
            "VALUES ('manual', 'ok', 'Has Pic', 'https://example.com/p.jpg') RETURNING id"
        ).fetchone()["id"]
        null_img = conn.execute(
            "INSERT INTO recipes (source, source_id, name, image_url) "
            "VALUES ('manual', 'null', 'No Pic', NULL) RETURNING id"
        ).fetchone()["id"]
        blank_img = conn.execute(
            "INSERT INTO recipes (source, source_id, name, image_url) "
            "VALUES ('manual', 'blank', 'Blank Pic', '   ') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, original_text) VALUES (?, 'x')",
            (null_img,),
        )
        conn.execute("INSERT INTO recipe_tags VALUES (?, 'tag')", (blank_img,))

        conn.executescript(migration)

        kept = {r["id"] for r in conn.execute("SELECT id FROM recipes")}
        assert kept == {ok}

        orphans = conn.execute(
            "SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id IN (?, ?)",
            (null_img, blank_img),
        ).fetchone()[0]
        assert orphans == 0
        orphan_tags = conn.execute(
            "SELECT COUNT(*) FROM recipe_tags WHERE recipe_id IN (?, ?)",
            (null_img, blank_img),
        ).fetchone()[0]
        assert orphan_tags == 0

        fts_ids = {r[0] for r in conn.execute("SELECT rowid FROM recipes_fts").fetchall()}
        assert null_img not in fts_ids and blank_img not in fts_ids


def test_get_connection_pragmas(db_path):
    with get_connection(db_path) as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert fk == 1
    assert wal.lower() == "wal"
    assert bt == 5000


def test_schema_check_constraints_enforced(db_path):
    """CHECK constraints should reject invalid values.

    `source` is intentionally free-form (validated at the application layer
    so plugin-provided importers can register new source names), so it is
    not covered here.
    """
    import sqlite3

    with connect(db_path) as conn:
        # bad rating
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO recipes (source, name, rating) VALUES ('manual', 'X', 99.0)")

    with connect(db_path) as conn:
        # bad nutrition_json
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO recipes (source, name, nutrition_json) "
                "VALUES ('manual', 'X', 'not-json')"
            )


def test_pantry_blocks_canonical_delete(db_path):
    """ON DELETE RESTRICT should prevent canonical deletion if pantry references it."""
    import sqlite3

    with connect(db_path) as conn:
        canonical_id = conn.execute(
            "SELECT id FROM canonical_ingredients ORDER BY id LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO pantry (canonical_id, quantity, unit) VALUES (?, 1.0, 'oz')",
            (canonical_id,),
        )

    with connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM canonical_ingredients WHERE id=?", (canonical_id,))


def test_fts_trigger_inserts_and_deletes(db_path):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO recipes (source, source_id, name, instructions_md) "
            "VALUES ('manual', 'fts-1', 'Lemon Pepper Chicken', 'sear in pan')"
        )

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'lemon'"
        ).fetchall()
    assert len(rows) == 1

    with connect(db_path) as conn:
        conn.execute("DELETE FROM recipes WHERE source_id='fts-1'")

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'lemon'"
        ).fetchall()
    assert len(rows) == 0


def test_fts_trigger_updates_only_on_relevant_columns(db_path):
    """Updating rating shouldn't churn FTS index — only name/instructions_md should."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO recipes (source, source_id, name, instructions_md, rating) "
            "VALUES ('manual', 'fts-2', 'Garlic Soup', 'simmer', 3.0)"
        )

    # Touching rating must not break the FTS row.
    with connect(db_path) as conn:
        conn.execute("UPDATE recipes SET rating=4.5 WHERE source_id='fts-2'")

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'garlic'"
        ).fetchall()
    assert len(rows) == 1

    # Touching name must update the FTS row.
    with connect(db_path) as conn:
        conn.execute("UPDATE recipes SET name='Onion Soup' WHERE source_id='fts-2'")

    with connect(db_path) as conn:
        garlic = conn.execute(
            "SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'garlic'"
        ).fetchall()
        onion = conn.execute(
            "SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'onion'"
        ).fetchall()
    assert len(garlic) == 0
    assert len(onion) == 1


def test_run_migrations_applies_unknown_files_once(tmp_path):
    db = tmp_path / "app.db"
    init_db(db_path=db)

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # Pick a version higher than any real migration so user_version gating allows it.
    m = migrations / "9001_test.sql"
    m.write_text("CREATE TABLE IF NOT EXISTS migration_marker (id INTEGER PRIMARY KEY);")

    with connect(db) as conn:
        applied = run_migrations(conn, migrations_dir=migrations)
    assert applied == ["9001_test.sql"]

    with connect(db) as conn:
        applied2 = run_migrations(conn, migrations_dir=migrations)
    assert applied2 == []


def test_run_migrations_sets_user_version(tmp_path):
    """After applying real migrations, PRAGMA user_version reflects the highest version."""
    db = tmp_path / "app.db"
    init_db(db_path=db)
    with connect(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    # user_version tracks the highest migration on disk.
    from pantry_cooking_vibes.db import _MIGRATIONS_DIR

    expected = max(int(p.name.split("_", 1)[0]) for p in _MIGRATIONS_DIR.glob("*.sql"))
    assert version == expected


def test_run_migrations_skips_when_user_version_advanced(tmp_path):
    """A migration whose version <= user_version is skipped even if its filename is unrecorded."""
    db = tmp_path / "app.db"
    init_db(db_path=db)

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # Lower version than current user_version (=2) — should be skipped.
    (migrations / "0001_old.sql").write_text(
        "CREATE TABLE IF NOT EXISTS should_not_exist (id INTEGER PRIMARY KEY);"
    )

    with connect(db) as conn:
        applied = run_migrations(conn, migrations_dir=migrations)
    assert applied == []

    with connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "should_not_exist" not in tables


def test_run_migrations_legacy_db_syncs_user_version(tmp_path):
    """A pre-existing DB with rows in schema_migrations but user_version=0
    should auto-bump user_version to the highest recorded version on first run,
    and not re-apply already-recorded migrations."""

    from pantry_cooking_vibes.db import apply_schema, get_connection, run_migrations

    db = tmp_path / "legacy.db"
    conn = get_connection(db)
    try:
        apply_schema(conn)
        # Manually populate schema_migrations as the old code would have, with
        # user_version still at 0.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute("INSERT INTO schema_migrations (filename) VALUES ('001_recipe_favorites.sql')")
        conn.commit()
    finally:
        conn.close()

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_recipe_favorites.sql").write_text(
        "CREATE TABLE IF NOT EXISTS recipe_favorites_legacy_marker (recipe_id INTEGER);"
    )
    (migrations / "002_new.sql").write_text(
        "CREATE TABLE IF NOT EXISTS new_marker (id INTEGER PRIMARY KEY);"
    )

    with connect(db) as conn:
        applied = run_migrations(conn, migrations_dir=migrations)
    # Recorded one (v1) is skipped; only the new v2 runs.
    assert applied == ["002_new.sql"]

    with connect(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2

    # Legacy-marker table from v1 should NOT exist (we skipped it).
    with connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "recipe_favorites_legacy_marker" not in tables
    assert "new_marker" in tables


def test_migration_filename_without_version_raises(tmp_path):
    """A migration filename missing the NNN_ prefix is a programmer bug — surface it."""
    db = tmp_path / "app.db"
    init_db(db_path=db)

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "no_prefix.sql").write_text("SELECT 1;")

    with connect(db) as conn:
        with pytest.raises(ValueError, match="must start with NNN_"):
            run_migrations(conn, migrations_dir=migrations)


def test_seed_loader_idempotent(tmp_path):
    db = tmp_path / "app.db"
    with connect(db) as conn:
        apply_schema(conn)
        first = seed_canonical_ingredients(conn)
        second = seed_canonical_ingredients(conn)
    assert first > 0
    assert second == 0


# ---------- migration 005 ----------


def test_migration_005_applied_idempotent(db_path):
    """Migration 005 is already applied by init_db. Re-running returns []."""
    with connect(db_path) as conn:
        applied = run_migrations(conn)
    assert applied == []

    with connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "meal_plan_favorites" in tables

    with connect(db_path) as conn:
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_meal_plans_week_draft" in indexes


# ---------- migration 006 ----------


def test_migration_006_adds_freshness_days_column(db_path):
    with connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(canonical_ingredients)")}
    assert "freshness_days" in cols


def test_migration_006_sets_freshness_days_for_known_categories(db_path):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT freshness_days FROM canonical_ingredients WHERE name = 'broccoli'"
        ).fetchone()
    assert row is not None
    assert row["freshness_days"] == 5  # broccoli: 3-5 days fridge


def test_migration_006_protein_freshness(db_path):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT freshness_days FROM canonical_ingredients WHERE name = 'chicken breast'"
        ).fetchone()
    assert row is not None
    assert row["freshness_days"] == 2  # raw chicken: 1-2 days fridge


def test_seed_populates_freshness_days_on_fresh_db(db_path):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT freshness_days FROM canonical_ingredients WHERE freshness_days IS NULL"
        ).fetchall()
    assert len(rows) == 0


def test_meal_plans_partial_unique_index_blocks_duplicate_drafts(db_path):
    """The partial unique index prevents two draft plans for the same week_of."""
    import sqlite3

    with connect(db_path) as conn:
        conn.execute("INSERT INTO meal_plans (week_of, status) VALUES ('2026-05-04', 'draft')")

    with pytest.raises(sqlite3.IntegrityError):
        with connect(db_path) as conn:
            conn.execute("INSERT INTO meal_plans (week_of, status) VALUES ('2026-05-04', 'draft')")


def test_meal_plans_partial_unique_index_allows_confirmed_plus_draft(db_path):
    """A confirmed + draft plan for the same week_of is allowed (index only constrains drafts)."""
    with connect(db_path) as conn:
        conn.execute("INSERT INTO meal_plans (week_of, status) VALUES ('2026-05-11', 'confirmed')")
        conn.execute("INSERT INTO meal_plans (week_of, status) VALUES ('2026-05-11', 'draft')")
        count = conn.execute(
            "SELECT COUNT(*) FROM meal_plans WHERE week_of = '2026-05-11'"
        ).fetchone()[0]
    assert count == 2
