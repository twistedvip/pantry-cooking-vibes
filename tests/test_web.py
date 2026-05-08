"""Tests for the Phase 7 FastAPI read-only web UI (pantry is read-write)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.web.app import create_app


@pytest.fixture
def client(seeded_db_path) -> TestClient:
    app = create_app(db_path=seeded_db_path)
    return TestClient(app)


# ---------- home ----------


def test_home_shows_counts(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Pantry Cooking Vibes" in body
    # seeded_db_path has 2 recipes, 1 pantry item, 0 plans, >0 canonical ingredients
    assert "Recipes" in body and "Pantry" in body and "Plans" in body
    assert ">2<" in body or "2</span>" in body  # recipe count
    assert ">1<" in body or "1</span>" in body  # pantry count


def test_static_mounted(client: TestClient):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


# ---------- recipes ----------


def test_recipes_list(client: TestClient):
    r = client.get("/recipes")
    assert r.status_code == 200
    assert "Broccoli Stir Fry" in r.text
    assert "Broccoli Soup" in r.text


def test_recipes_search_narrows(client: TestClient):
    r = client.get("/recipes", params={"q": "soup"})
    assert r.status_code == 200
    assert "Broccoli Soup" in r.text
    # stir fry should not appear for "soup" query
    assert "Stir Fry" not in r.text


def test_recipes_filter_max_time(client: TestClient):
    r = client.get("/recipes", params={"max_time": 30})
    assert r.status_code == 200
    assert "Stir Fry" in r.text  # 25 min
    assert "Broccoli Soup" not in r.text  # 60 min


def test_recipes_filter_tags(client: TestClient):
    r = client.get("/recipes", params={"tags": "soup"})
    assert r.status_code == 200
    assert "Broccoli Soup" in r.text
    assert "Stir Fry" not in r.text


def test_recipes_empty_results_shows_empty_state(client: TestClient):
    r = client.get("/recipes", params={"q": "zzzznomatchzzzz"})
    assert r.status_code == 200
    assert "No recipes match" in r.text


def test_recipes_search_with_blank_numeric_fields(client: TestClient):
    """Regression: the HTML form sends max_time= and limit= as empty strings
    when the user only types a text query. Those must coerce to defaults
    instead of triggering a 422 'expects integer' error."""
    r = client.get("/recipes", params={"q": "broccoli", "max_time": "", "tags": "", "limit": ""})
    assert r.status_code == 200
    assert "Broccoli" in r.text


def test_recipes_search_rejects_non_integer_max_time(client: TestClient):
    """A genuinely non-numeric max_time should still be rejected (422)."""
    r = client.get("/recipes", params={"q": "broccoli", "max_time": "chicken"})
    assert r.status_code == 422


def test_recipes_filter_by_single_source(client: TestClient):
    r = client.get("/recipes", params={"sources": "manual"})
    assert r.status_code == 200
    assert "Stir Fry" in r.text  # source='manual'
    assert "Broccoli Soup" not in r.text  # source='url'


def test_recipes_filter_by_multiple_sources(client: TestClient):
    r = client.get("/recipes", params=[("sources", "manual"), ("sources", "url")])
    assert r.status_code == 200
    assert "Stir Fry" in r.text
    assert "Broccoli Soup" in r.text


def test_recipes_no_source_filter_shows_all(client: TestClient):
    """Default (no sources param) must not restrict by source."""
    r = client.get("/recipes")
    assert r.status_code == 200
    assert "Stir Fry" in r.text
    assert "Broccoli Soup" in r.text


def test_recipes_source_checkboxes_render(client: TestClient):
    r = client.get("/recipes")
    assert 'name="sources" value="manual"' in r.text
    assert 'name="sources" value="url"' in r.text


def test_recipes_limit_invalid_falls_back_to_default(client: TestClient):
    """A limit not in {50, 100, 250} should silently fall back to 50, not error."""
    r = client.get("/recipes", params={"limit": "37"})
    assert r.status_code == 200
    # The select renders selected=50 when input was off-menu.
    assert '<option value="50" selected>50</option>' in r.text


def test_recipes_filter_by_ingredient(client: TestClient):
    r = client.get("/recipes", params={"ingredients": "broccoli"})
    assert r.status_code == 200
    assert "Broccoli Stir Fry" in r.text
    assert "Broccoli Soup" in r.text


def test_recipes_filter_by_unknown_ingredient_empty(client: TestClient):
    r = client.get("/recipes", params={"ingredients": "zzznoingredientzzz"})
    assert r.status_code == 200
    assert "No recipes match" in r.text


def test_recipes_filter_by_ingredients_and(client: TestClient, seeded_db_path):
    """AND mode: requesting broccoli + the 'other' canonical only matches Stir Fry."""
    with connect(seeded_db_path) as conn:
        other = conn.execute(
            "SELECT name FROM canonical_ingredients WHERE name != 'broccoli' ORDER BY id LIMIT 1"
        ).fetchone()["name"]
    r = client.get(
        "/recipes",
        params={"ingredients": f"broccoli,{other}", "ingredient_mode": "and"},
    )
    assert r.status_code == 200
    assert "Broccoli Stir Fry" in r.text
    assert "Broccoli Soup" not in r.text


def test_recipes_filter_pantry_only(client: TestClient):
    """Only Soup has all mapped ingredients in the pantry (broccoli only;
    its other ingredient is unmapped → ignored)."""
    r = client.get("/recipes", params={"pantry_only": "1"})
    assert r.status_code == 200
    assert "Broccoli Soup" in r.text
    assert "Broccoli Stir Fry" not in r.text


def test_recipes_ingredient_filter_renders_inputs(client: TestClient):
    r = client.get("/recipes")
    assert r.status_code == 200
    assert 'name="ingredients"' in r.text
    assert 'name="ingredient_mode"' in r.text
    assert 'name="pantry_only"' in r.text


def test_recipe_detail(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        recipe_id = conn.execute(
            "SELECT id FROM recipes WHERE name='Broccoli Stir Fry'"
        ).fetchone()["id"]
    r = client.get(f"/recipes/{recipe_id}")
    assert r.status_code == 200
    assert "Broccoli Stir Fry" in r.text
    assert "Ingredients" in r.text
    assert "quick" in r.text and "asian" in r.text
    assert "Stir fry broccoli" in r.text  # instructions


def test_recipe_detail_shows_delete_button(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert f'action="/recipes/{rid}/delete"' in r.text
    assert "Delete recipe" in r.text


def test_delete_recipe_redirects_to_list_and_removes_row(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]

    r = client.post(f"/recipes/{rid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/recipes"

    with connect(seeded_db_path) as conn:
        gone = conn.execute("SELECT 1 FROM recipes WHERE id = ?", (rid,)).fetchone()
    assert gone is None

    # Detail page now 404s.
    r2 = client.get(f"/recipes/{rid}")
    assert r2.status_code == 404


def test_delete_missing_recipe_returns_404(client: TestClient):
    r = client.post("/recipes/99999/delete", follow_redirects=False)
    assert r.status_code == 404


def test_recipe_detail_missing_returns_404(client: TestClient):
    r = client.get("/recipes/99999")
    assert r.status_code == 404


# ---------- favorites ----------


def _recipe_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM recipes WHERE name = ?", (name,)).fetchone()["id"]


def test_favorite_toggle_round_trip(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")

    r = client.post(
        f"/recipes/{rid}/favorite",
        data={"favorite": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/recipes/{rid}"

    with connect(seeded_db_path) as conn:
        assert (
            conn.execute("SELECT 1 FROM recipe_favorites WHERE recipe_id = ?", (rid,)).fetchone()
            is not None
        )

    # Unfavorite
    r = client.post(
        f"/recipes/{rid}/favorite",
        data={"favorite": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with connect(seeded_db_path) as conn:
        assert (
            conn.execute("SELECT 1 FROM recipe_favorites WHERE recipe_id = ?", (rid,)).fetchone()
            is None
        )


def test_favorite_missing_recipe_returns_404(client: TestClient):
    r = client.post("/recipes/99999/favorite", data={"favorite": "1"})
    assert r.status_code == 404


def test_favorite_redirect_preserves_list_filter(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Soup")
    r = client.post(
        f"/recipes/{rid}/favorite",
        data={"favorite": "1", "redirect_to": "/recipes?q=soup"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/recipes?q=soup"


def test_favorite_redirect_rejects_external_url(client: TestClient, seeded_db_path):
    """redirect_to must be a same-origin path, not an arbitrary URL."""
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")
    r = client.post(
        f"/recipes/{rid}/favorite",
        data={"favorite": "1", "redirect_to": "https://evil.example.com/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Should fall back to the recipe detail, not follow the external URL
    assert r.headers["location"] == f"/recipes/{rid}"


def test_favorites_filter_narrows_list(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        stir_fry = _recipe_id(conn, "Broccoli Stir Fry")

    # With no favorites set, fav=1 returns empty results
    r = client.get("/recipes", params={"fav": "1"})
    assert r.status_code == 200
    assert "Stir Fry" not in r.text
    assert "Broccoli Soup" not in r.text
    assert "No recipes match" in r.text

    # Favorite the stir fry, then filter
    client.post(f"/recipes/{stir_fry}/favorite", data={"favorite": "1"})
    r = client.get("/recipes", params={"fav": "1"})
    assert r.status_code == 200
    assert "Stir Fry" in r.text
    assert "Broccoli Soup" not in r.text


def test_favorite_state_visible_in_detail(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")

    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    # Unfavorited initially — outline star in label
    assert "☆ favorite" in r.text

    client.post(f"/recipes/{rid}/favorite", data={"favorite": "1"})
    r = client.get(f"/recipes/{rid}")
    assert "★ favorited" in r.text


# ---------- pantry (read-write) ----------


def test_pantry_page_lists_items(client: TestClient):
    r = client.get("/pantry")
    assert r.status_code == 200
    assert "broccoli" in r.text
    assert "In your pantry (1)" in r.text


def test_pantry_search_shows_suggestions(client: TestClient):
    r = client.get("/pantry", params={"search": "brocc"})
    assert r.status_code == 200
    assert "broccoli" in r.text
    # suggestion row has an add button
    assert "canonical_id" in r.text


def test_pantry_search_no_match_shows_empty(client: TestClient):
    r = client.get("/pantry", params={"search": "zzzznoingredientzzz"})
    assert r.status_code == 200
    assert "No canonical ingredient matches" in r.text


def test_pantry_add_item_flow(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        cid = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name != 'broccoli' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        before = conn.execute("SELECT COUNT(*) FROM pantry").fetchone()[0]

    r = client.post(
        "/pantry/add",
        data={"canonical_id": str(cid), "quantity": "2.5", "unit": "lb", "note": "fresh"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/pantry?added=" in r.headers["location"]

    with connect(seeded_db_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM pantry").fetchone()[0]
        row = conn.execute(
            "SELECT canonical_id, quantity, unit, note FROM pantry WHERE canonical_id=? "
            "ORDER BY id DESC LIMIT 1",
            (cid,),
        ).fetchone()
    assert after == before + 1
    assert row["quantity"] == 2.5
    assert row["unit"] == "lb"
    assert row["note"] == "fresh"


def test_pantry_add_negative_quantity_redirects_with_error(client: TestClient):
    r = client.post(
        "/pantry/add",
        data={"canonical_id": "1", "quantity": "-1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_pantry_update_item_quantity_and_unit(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        item_id = conn.execute("SELECT id FROM pantry LIMIT 1").fetchone()["id"]

    r = client.post(
        f"/pantry/{item_id}/update",
        data={"quantity": "3.5", "unit": "lb"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert f"/pantry?updated=item%20{item_id}" in r.headers["location"]

    with connect(seeded_db_path) as conn:
        row = conn.execute("SELECT quantity, unit FROM pantry WHERE id = ?", (item_id,)).fetchone()
    assert row["quantity"] == 3.5
    assert row["unit"] == "lb"


def test_pantry_update_blank_unit_clears_to_null(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        item_id = conn.execute("SELECT id FROM pantry LIMIT 1").fetchone()["id"]

    r = client.post(
        f"/pantry/{item_id}/update",
        data={"quantity": "1", "unit": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with connect(seeded_db_path) as conn:
        unit = conn.execute("SELECT unit FROM pantry WHERE id = ?", (item_id,)).fetchone()["unit"]
    assert unit is None


def test_pantry_update_negative_quantity_redirects_with_error(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        item_id = conn.execute("SELECT id FROM pantry LIMIT 1").fetchone()["id"]

    r = client.post(
        f"/pantry/{item_id}/update",
        data={"quantity": "-2", "unit": "lb"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_pantry_update_missing_redirects_with_error(client: TestClient):
    r = client.post(
        "/pantry/99999/update",
        data={"quantity": "1", "unit": "lb"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_pantry_page_renders_inline_edit_form(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        item_id = conn.execute("SELECT id FROM pantry LIMIT 1").fetchone()["id"]
    r = client.get("/pantry")
    assert r.status_code == 200
    assert f'action="/pantry/{item_id}/update"' in r.text
    # Both quantity and unit inputs present
    assert 'name="quantity"' in r.text
    assert 'name="unit"' in r.text


def test_recipe_detail_tooltip_shows_canonical_name(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    # Stir fry has broccoli (in pantry) → tooltip should mention canonical name
    assert "in pantry (broccoli)" in r.text


def test_recipe_detail_tooltip_for_unmapped(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Soup'").fetchone()["id"]
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    # Soup has unmapped 'vegetable stock'
    assert 'title="unmapped"' in r.text


def test_pantry_delete_item(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        item_id = conn.execute("SELECT id FROM pantry LIMIT 1").fetchone()["id"]

    r = client.post(f"/pantry/{item_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "/pantry?removed=" in r.headers["location"]

    with connect(seeded_db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM pantry").fetchone()[0]
    assert count == 0


def test_pantry_delete_missing_redirects_with_error(client: TestClient):
    r = client.post("/pantry/99999/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


# ---------- plans ----------


def test_plans_list_empty(client: TestClient):
    r = client.get("/plans")
    assert r.status_code == 200
    assert "No meal plans yet" in r.text


def test_plans_list_and_detail(client: TestClient, seeded_db_path):
    # Insert a meal plan + one item so the page has something to render.
    with connect(seeded_db_path) as conn:
        recipe_id = conn.execute(
            "SELECT id FROM recipes WHERE name='Broccoli Stir Fry'"
        ).fetchone()["id"]
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of, status, notes) "
            "VALUES ('2026-04-20', 'draft', 'quick week') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id, day, meal_slot, servings_planned) "
            "VALUES (?, ?, 'mon', 'dinner', 2)",
            (plan_id, recipe_id),
        )

    r = client.get("/plans")
    assert r.status_code == 200
    assert "2026-04-20" in r.text
    assert "draft" in r.text

    r = client.get(f"/plans/{plan_id}")
    assert r.status_code == 200
    assert "Broccoli Stir Fry" in r.text
    assert "mon" in r.text and "dinner" in r.text


def test_plan_detail_missing_returns_404(client: TestClient):
    r = client.get("/plans/99999")
    assert r.status_code == 404


def test_plan_shopping_list(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-05-04') RETURNING id"
        ).fetchone()["id"]
        for rid in conn.execute("SELECT id FROM recipes").fetchall():
            conn.execute(
                "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)",
                (plan_id, rid["id"]),
            )

    r = client.get(f"/plans/{plan_id}/shopping")
    assert r.status_code == 200
    # broccoli is in pantry → should appear in covered section, not needed
    assert "Already in pantry" in r.text
    assert "Need to buy" in r.text
    # unmapped 'vegetable stock' string should be surfaced
    assert "stock" in r.text


def test_plan_shopping_missing_plan_returns_404(client: TestClient):
    r = client.get("/plans/99999/shopping")
    assert r.status_code == 404


# ---------- CLI wiring ----------


def test_serve_web_cli_missing_db_exits_cleanly(tmp_path):
    """serve-web must fail fast with a helpful message when the DB is missing."""
    from typer.testing import CliRunner

    from pantry_cooking_vibes.cli import app as cli_app

    runner = CliRunner()
    missing = tmp_path / "does_not_exist.db"
    result = runner.invoke(cli_app, ["serve-web", "--db", str(missing)])
    assert result.exit_code == 1
    assert "Database not found" in result.output


def test_app_factory_importable():
    """The uvicorn target string used by the CLI must import cleanly."""
    from pantry_cooking_vibes.web import app_factory

    assert app_factory.app is not None


def test_serve_web_applies_pending_migrations(tmp_path, monkeypatch):
    """Regression: a DB created before migrations were tracked (schema applied,
    schema_migrations empty, user_version=0) used to crash. serve-web must
    bring the DB up to date before uvicorn starts.

    Even though schema.sql now creates the favorites tables directly (so a
    fresh DB doesn't need migrations), this test still verifies that the
    serve-web bootstrap runs the migration sweep against a stale DB.
    """
    from typer.testing import CliRunner

    from pantry_cooking_vibes.cli import app as cli_app
    from pantry_cooking_vibes.db import apply_schema, get_connection

    # Simulate the pre-migration state: schema applied, schema_migrations empty,
    # user_version reset to 0 (the legacy state migrations are designed to repair).
    db = tmp_path / "stale.db"
    conn = get_connection(db)
    try:
        apply_schema(conn)
        conn.execute("PRAGMA user_version = 0")
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        conn.commit()
    finally:
        conn.close()

    with get_connection(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 0, "precondition: DB is pre-migration"

    # Stub uvicorn so serve-web runs its bootstrap without actually binding a port.
    invoked = {}

    def fake_run(*args, **kwargs):
        invoked["ran"] = True

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = CliRunner().invoke(cli_app, ["serve-web", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert invoked.get("ran") is True
    assert "Applied pending migrations" in result.output
    assert "001_recipe_favorites.sql" in result.output

    # After serve-web bootstrap, user_version reflects the highest applied migration.
    with get_connection(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        recorded = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    assert version > 0
    assert "001_recipe_favorites.sql" in recorded


def test_db_backup_missing_source_exits_cleanly(tmp_path):
    """Regression: db-backup against a missing source used to surface
    sqlite's cryptic 'unable to open database file'. It should now exit 1
    with a helpful message instead."""
    from typer.testing import CliRunner

    from pantry_cooking_vibes.cli import app as cli_app

    runner = CliRunner()
    missing = tmp_path / "nope.db"
    dest = tmp_path / "backup.db"
    result = runner.invoke(cli_app, ["db-backup", str(dest), "--db", str(missing)])
    assert result.exit_code == 1
    assert "Database not found" in result.output


def test_db_backup_into_directory(tmp_path, seeded_db_path):
    """Regression: ``meal-cli db-backup ./db_backups/`` used to crash with
    'unable to open database file' because sqlite3 can't open a directory.
    A directory dest should instead produce a timestamped file inside it."""
    from typer.testing import CliRunner

    from pantry_cooking_vibes.cli import app as cli_app

    backup_dir = tmp_path / "db_backups"
    backup_dir.mkdir()
    runner = CliRunner()
    # Trailing separator in the argument mirrors the user's invocation.
    result = runner.invoke(
        cli_app,
        ["db-backup", str(backup_dir) + "/", "--db", str(seeded_db_path)],
    )
    assert result.exit_code == 0, result.output
    produced = list(backup_dir.glob("*.db"))
    assert len(produced) == 1
    assert produced[0].stat().st_size > 0


def test_db_backup_round_trip(tmp_path, seeded_db_path):
    """db-backup should produce a readable SQLite file containing the
    same recipe rows as the source."""
    from typer.testing import CliRunner

    from pantry_cooking_vibes.cli import app as cli_app
    from pantry_cooking_vibes.db import connect

    dest = tmp_path / "nested" / "backup.db"
    runner = CliRunner()
    result = runner.invoke(cli_app, ["db-backup", str(dest), "--db", str(seeded_db_path)])
    assert result.exit_code == 0, result.output
    assert dest.exists()
    with connect(dest) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM recipes")}
    assert "Broccoli Stir Fry" in names and "Broccoli Soup" in names


# ---------- mappings (read-write) ----------


def _seed_queue(db_path):
    """Seed the mapping queue with one proposed + one no_match row.

    Returns (proposed_id, no_match_id, broccoli_canonical_id).
    """
    with connect(db_path) as conn:
        broccoli_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name='broccoli'"
        ).fetchone()["id"]
        proposed_id = conn.execute(
            "INSERT INTO ingredient_mapping_queue "
            "(source, source_key, original_text, proposed_canonical_id, confidence, status) "
            "VALUES ('test-source', 'broc-florets-bag', "
            "        'Broccoli Florets, 1 bag', ?, 0.84, 'proposed') RETURNING id",
            (broccoli_id,),
        ).fetchone()["id"]
        no_match_id = conn.execute(
            "INSERT INTO ingredient_mapping_queue "
            "(source, source_key, original_text, proposed_canonical_id, confidence, status) "
            "VALUES ('url_import', 'mystery-thing', "
            "        '1 cup of mystery thing', NULL, 0.0, 'proposed') RETURNING id"
        ).fetchone()["id"]
    return proposed_id, no_match_id, broccoli_id


def test_mappings_list_default_shows_proposed(client: TestClient, seeded_db_path):
    proposed_id, no_match_id, _ = _seed_queue(seeded_db_path)
    r = client.get("/mappings")
    assert r.status_code == 200
    assert "Broccoli Florets, 1 bag" in r.text
    assert "mystery thing" in r.text
    assert "no match" in r.text
    assert "broccoli" in r.text  # proposed canonical name


def test_mappings_filter_by_source(client: TestClient, seeded_db_path):
    _seed_queue(seeded_db_path)
    r = client.get("/mappings", params={"source": "url_import"})
    assert r.status_code == 200
    assert "mystery thing" in r.text
    assert "Broccoli Florets" not in r.text


def test_mappings_invalid_status_redirects_with_error(client: TestClient):
    r = client.get("/mappings", params={"status": "bogus"}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_mapping_detail_renders(client: TestClient, seeded_db_path):
    proposed_id, _, _ = _seed_queue(seeded_db_path)
    r = client.get(f"/mappings/{proposed_id}")
    assert r.status_code == 200
    assert "Broccoli Florets, 1 bag" in r.text
    assert "Approve as proposed" in r.text


def test_mapping_detail_missing_returns_404(client: TestClient):
    r = client.get("/mappings/99999")
    assert r.status_code == 404


def test_mapping_detail_search_lists_canonicals(client: TestClient, seeded_db_path):
    _, no_match_id, _ = _seed_queue(seeded_db_path)
    r = client.get(f"/mappings/{no_match_id}", params={"search": "brocc"})
    assert r.status_code == 200
    assert "broccoli" in r.text
    assert "Approve as broccoli" in r.text


def test_mapping_approve_uses_proposed_canonical(client: TestClient, seeded_db_path):
    proposed_id, _, broccoli_id = _seed_queue(seeded_db_path)
    r = client.post(f"/mappings/{proposed_id}/approve", follow_redirects=False)
    assert r.status_code == 303
    assert "approved=" in r.headers["location"]
    with connect(seeded_db_path) as conn:
        row = conn.execute(
            "SELECT status, proposed_canonical_id FROM ingredient_mapping_queue WHERE id=?",
            (proposed_id,),
        ).fetchone()
    assert row["status"] == "approved"
    assert row["proposed_canonical_id"] == broccoli_id


def test_mapping_approve_with_override(client: TestClient, seeded_db_path):
    _, no_match_id, broccoli_id = _seed_queue(seeded_db_path)
    r = client.post(
        f"/mappings/{no_match_id}/approve",
        data={"canonical_id": str(broccoli_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "approved=" in r.headers["location"]
    with connect(seeded_db_path) as conn:
        row = conn.execute(
            "SELECT status, proposed_canonical_id FROM ingredient_mapping_queue WHERE id=?",
            (no_match_id,),
        ).fetchone()
    assert row["status"] == "approved"
    assert row["proposed_canonical_id"] == broccoli_id


def test_mapping_approve_no_match_without_pick_redirects_with_error(
    client: TestClient, seeded_db_path
):
    _, no_match_id, _ = _seed_queue(seeded_db_path)
    r = client.post(f"/mappings/{no_match_id}/approve", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert f"/mappings/{no_match_id}" in loc and "error=" in loc


def test_mapping_reject(client: TestClient, seeded_db_path):
    proposed_id, _, _ = _seed_queue(seeded_db_path)
    r = client.post(f"/mappings/{proposed_id}/reject", follow_redirects=False)
    assert r.status_code == 303
    assert "rejected=" in r.headers["location"]
    with connect(seeded_db_path) as conn:
        status = conn.execute(
            "SELECT status FROM ingredient_mapping_queue WHERE id=?", (proposed_id,)
        ).fetchone()["status"]
    assert status == "rejected"


def test_mapping_approve_missing_redirects_with_error(client: TestClient):
    r = client.post("/mappings/99999/approve", follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


# ---------- plan authoring ----------


def test_post_plans_creates_plan_for_current_sunday(client: TestClient, seeded_db_path):
    r = client.post("/plans", data={}, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/plans/")
    plan_id = int(location.split("/plans/")[1])

    with connect(seeded_db_path) as conn:
        plan = conn.execute(
            "SELECT week_of, status FROM meal_plans WHERE id = ?", (plan_id,)
        ).fetchone()
    assert plan["status"] == "draft"
    from pantry_cooking_vibes.dates import current_sunday

    assert plan["week_of"] == current_sunday().isoformat()


def test_post_plans_rejects_non_sunday(client: TestClient):
    r = client.post("/plans", data={"week_of": "2026-05-06"}, follow_redirects=False)
    assert r.status_code == 422


def test_post_recipe_add_to_current_week_redirects_and_appends(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]

    r = client.post(f"/recipes/{rid}/add-to-current-week", data={}, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert "/plans/" in location

    plan_id = int(location.split("/plans/")[1])
    with connect(seeded_db_path) as conn:
        items = conn.execute(
            "SELECT recipe_id FROM meal_plan_items WHERE plan_id = ?", (plan_id,)
        ).fetchall()
    assert any(i["recipe_id"] == rid for i in items)


def test_post_recipe_add_to_current_week_missing_recipe(client: TestClient):
    r = client.post("/recipes/99999/add-to-current-week", data={}, follow_redirects=False)
    assert r.status_code == 404


def test_post_plan_favorite_toggle(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of, status) VALUES ('2026-05-04', 'draft') RETURNING id"
        ).fetchone()["id"]

    r = client.post(f"/plans/{plan_id}/favorite", data={"favorite": "1"}, follow_redirects=False)
    assert r.status_code == 303

    with connect(seeded_db_path) as conn:
        fav = conn.execute(
            "SELECT 1 FROM meal_plan_favorites WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    assert fav is not None


def test_post_plan_clone_redirects_to_new_plan(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of, status, notes) "
            "VALUES ('2026-04-13', 'draft', 'src') RETURNING id"
        ).fetchone()["id"]
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)", (plan_id, rid)
        )

    r = client.post(f"/plans/{plan_id}/clone", data={}, follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].split("/plans/")[1])
    assert new_id != plan_id

    with connect(seeded_db_path) as conn:
        new_plan = conn.execute(
            "SELECT notes, status FROM meal_plans WHERE id = ?", (new_id,)
        ).fetchone()
    assert new_plan["notes"].startswith(f"Cloned from #{plan_id}.")
    assert new_plan["status"] == "draft"


def test_post_plan_item_delete_match(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-05-04') RETURNING id"
        ).fetchone()["id"]
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
        item_id = conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?) RETURNING id",
            (plan_id, rid),
        ).fetchone()["id"]

    r = client.post(f"/plans/{plan_id}/items/{item_id}/delete", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert f"/plans/{plan_id}" in r.headers["location"]

    with connect(seeded_db_path) as conn:
        gone = conn.execute("SELECT 1 FROM meal_plan_items WHERE id = ?", (item_id,)).fetchone()
    assert gone is None


def test_post_plan_item_delete_404_on_mismatch(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        p1 = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-04-06') RETURNING id"
        ).fetchone()["id"]
        p2 = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-04-13') RETURNING id"
        ).fetchone()["id"]
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
        item_id = conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?) RETURNING id",
            (p1, rid),
        ).fetchone()["id"]

    r = client.post(f"/plans/{p2}/items/{item_id}/delete", data={}, follow_redirects=False)
    assert r.status_code == 404

    with connect(seeded_db_path) as conn:
        still_there = conn.execute(
            "SELECT 1 FROM meal_plan_items WHERE id = ?", (item_id,)
        ).fetchone()
    assert still_there is not None


def test_get_plans_print_dom_structure(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-05-04') RETURNING id"
        ).fetchone()["id"]
        for rid in conn.execute("SELECT id FROM recipes").fetchall():
            conn.execute(
                "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)",
                (plan_id, rid["id"]),
            )

    r = client.get(f"/plans/{plan_id}/print")
    assert r.status_code == 200
    body = r.text
    assert 'data-role="ingredients-page"' in body
    assert body.count('data-role="recipe-page"') == 2
    assert "/static/print.css" in body
    assert "topbar" not in body
    assert "mainnav" not in body


def test_plan_list_renders_chips(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of, status) VALUES ('2026-05-04', 'draft') RETURNING id"
        ).fetchone()["id"]
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)", (plan_id, rid)
        )

    r = client.get("/plans")
    assert r.status_code == 200
    body = r.text
    assert "coverage-chip" in body
    assert "% pantry" in body
    assert "fav-toggle" in body or "fav-btn" in body
    assert "Clone" in body
    assert "/print" in body


# ---------- security middleware / headers ----------


def test_security_headers_set_on_responses(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "Content-Security-Policy" in r.headers
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_post_blocked_when_origin_does_not_match_host(client: TestClient, seeded_db_path):
    """A drive-by page on attacker.example must not be able to delete recipes
    via a cross-origin form POST. Origin/Referer mismatch -> 403."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/delete",
        headers={"origin": "http://attacker.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_allowed_when_origin_matches_host(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Soup'").fetchone()["id"]
    # TestClient defaults Host to testserver
    r = client.post(
        f"/recipes/{rid}/favorite",
        data={"favorite": "1"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_post_allowed_when_no_origin_or_referer(client: TestClient, seeded_db_path):
    """CLI / MCP clients don't send Origin or Referer; they must still work."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Soup'").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/favorite",
        data={"favorite": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# ---------- safe_redirect ----------


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/recipes/1", "/recipes/1"),
        ("/", "/"),
        ("//evil.example/x", "FALLBACK"),
        ("/\\evil.example/x", "FALLBACK"),
        ("https://evil.example/x", "FALLBACK"),
        ("javascript:alert(1)", "FALLBACK"),
        ("", "FALLBACK"),
        (None, "FALLBACK"),
    ],
)
def test_safe_redirect_rejects_cross_origin_targets(target, expected):
    from pantry_cooking_vibes.web.deps import safe_redirect

    assert safe_redirect(target, "FALLBACK") == expected


def test_protocol_relative_redirect_target_falls_back(client: TestClient, seeded_db_path):
    """Regression: redirect_to=//evil.example would have followed cross-origin."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Soup'").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/favorite",
        data={"favorite": "1", "redirect_to": "//evil.example/owned"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Must NOT redirect to attacker; falls back to recipe page.
    assert r.headers["location"] == f"/recipes/{rid}"
