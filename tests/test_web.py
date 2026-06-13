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


@pytest.fixture
def client_empty(db_path) -> TestClient:
    """Client on a schema-only DB (no recipes) for first-run home checks."""
    app = create_app(db_path=db_path)
    return TestClient(app)


# ---------- home ----------


def test_healthz_returns_ok_without_template_or_db(tmp_path):
    """`/healthz` must return 200 plain-text without rendering a template
    or touching the DB — succeed even when the DB file is missing."""
    from pantry_cooking_vibes.web.app import create_app

    missing_db = tmp_path / "does-not-exist.db"
    probe = TestClient(create_app(db_path=missing_db))

    r = probe.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_home_shows_counts(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Pantry Cooking Vibes" in body
    # seeded_db_path has 2 recipes, 1 pantry item, 0 plans.
    assert "Recipes" in body and "Pantry" in body and "Plans" in body
    assert ">2<" in body  # recipe count in the glance strip
    assert ">1<" in body  # pantry count in the glance strip


def test_home_shows_decision_sections(client: TestClient):
    """The home leads with decision sections, not a metric-card grid."""
    r = client.get("/")
    body = r.text
    assert "In the kitchen" in body
    assert "Ready to cook" in body
    assert "Use it soon" in body
    assert "This week" in body
    # The retired hero-metric scaffolding must be gone.
    assert "stat-card" not in body
    assert "canonical ingredients" not in body.lower()


def test_home_first_run_teaches_import(client_empty: TestClient):
    """With no recipes imported, the home onboards instead of showing zeros."""
    r = client_empty.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Start by importing recipes" in body
    assert "meal-cli import" in body


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


@pytest.mark.parametrize(
    "query",
    [
        "One-Pan Chicken Parm on Veggies",
        "name:foo",
        "spicy*",
        '"unterminated',
        "(broken",
    ],
)
def test_recipes_search_with_fts5_operator_chars_does_not_500(client: TestClient, query: str):
    # Regression for the production crash where FTS5 parsed a hyphen inside
    # the user's title query as a NOT operator and surfaced a 500
    # ("no such column: Pan" from "One-Pan Chicken ..."). Route should always
    # render a 200, even if the query yields no results.
    r = client.get("/recipes", params={"q": query})
    assert r.status_code == 200


def test_recipes_filter_max_time(client: TestClient):
    r = client.get("/recipes", params={"max_time": 30})
    assert r.status_code == 200
    assert "Stir Fry" in r.text  # 25 min
    assert "Broccoli Soup" not in r.text  # 60 min


def test_recipes_default_sort_is_availability(client: TestClient):
    r = client.get("/recipes")
    assert r.status_code == 200
    # The sort selector renders with "On-hand ingredients" pre-selected...
    assert 'name="sort"' in r.text
    assert 'value="availability" selected' in r.text
    # ...and the toolbar reports the on-hand ordering.
    assert "most ingredients on hand" in r.text


def test_recipes_sort_rating_switches_label(client: TestClient):
    r = client.get("/recipes", params={"sort": "rating"})
    assert r.status_code == 200
    assert 'value="rating" selected' in r.text
    assert "top-rated first" in r.text


def test_recipes_invalid_sort_falls_back_to_availability(client: TestClient):
    r = client.get("/recipes", params={"sort": "bogus"})
    assert r.status_code == 200
    assert 'value="availability" selected' in r.text


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


# ---------- recipes pagination ----------


@pytest.fixture
def bulk_recipes(seeded_db_path) -> None:
    """Pad the seeded DB to 62 recipes so the default 50-per-page paginates."""
    with connect(seeded_db_path) as conn:
        for i in range(60):
            conn.execute(
                "INSERT INTO recipes (source, source_id, name, rating) "
                "VALUES ('manual', ?, ?, 2.0)",
                (f"bulk-{i}", f"Bulk Recipe {i:02d}"),
            )


def test_recipes_paginates_past_first_page(client: TestClient, bulk_recipes):
    r1 = client.get("/recipes")
    assert r1.status_code == 200
    assert "Showing 1–50 of 62 recipes" in r1.text
    assert 'aria-current="page">1<' in r1.text
    assert "page=2" in r1.text

    r2 = client.get("/recipes", params={"page": 2})
    assert r2.status_code == 200
    assert "Showing 51–62 of 62 recipes" in r2.text
    assert 'aria-current="page">2<' in r2.text
    # Page 2 holds different cards than page 1.
    assert r2.text.count("recipe-card") < r1.text.count("recipe-card")


def test_recipes_page_links_preserve_filters(client: TestClient, bulk_recipes):
    r = client.get("/recipes", params={"sources": "manual"})
    assert r.status_code == 200
    # The next-page link carries the active source filter along.
    assert "sources=manual" in r.text and "page=2" in r.text


def test_recipes_page_past_end_clamps_to_last(client: TestClient, bulk_recipes):
    r = client.get("/recipes", params={"page": "999"})
    assert r.status_code == 200
    assert "Showing 51–62 of 62 recipes" in r.text


def test_recipes_page_invalid_values_rejected(client: TestClient):
    assert client.get("/recipes", params={"page": "abc"}).status_code == 422
    assert client.get("/recipes", params={"page": "0"}).status_code == 422


def test_recipes_no_pager_on_single_page(client: TestClient):
    """Two seeded recipes fit one page; the pager should not render at all."""
    r = client.get("/recipes")
    assert r.status_code == 200
    assert 'class="pager' not in r.text
    assert "2 results." in r.text


def test_limit_choices_stay_within_tools_cap():
    """The route's page math assumes tools honors every limit choice verbatim;
    a choice above MAX_RESULT_LIMIT would make deep pages unreachable."""
    from pantry_cooking_vibes.mcp_server import tools
    from pantry_cooking_vibes.web.routes import recipes as recipes_route

    assert max(recipes_route._LIMIT_CHOICES) <= tools.MAX_RESULT_LIMIT


def test_page_window_never_hides_a_single_page():
    from pantry_cooking_vibes.web.routes.recipes import _page_window

    # A gap of exactly one page renders the page, not an ellipsis.
    assert _page_window(5, 25) == [1, 2, 3, 4, 5, 6, 7, None, 25]
    assert _page_window(21, 25) == [1, None, 19, 20, 21, 22, 23, 24, 25]
    # Wider gaps still collapse.
    assert _page_window(6, 25) == [1, None, 4, 5, 6, 7, 8, None, 25]
    # Short runs render in full.
    assert _page_window(1, 7) == [1, 2, 3, 4, 5, 6, 7]


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


def test_recipe_detail_caps_tags_behind_disclosure(client: TestClient, seeded_db_path):
    """>8 tags: show the first 8, tuck the rest behind a '+N more tags'
    disclosure, and drop nothing. Tags render ordered by name."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute(
            "INSERT INTO recipes (source, source_id, name) "
            "VALUES ('manual', 'many-tags', 'Many Tag Recipe') RETURNING id"
        ).fetchone()["id"]
        for i in range(1, 13):  # tag01..tag12 (12 tags, already alphabetical)
            conn.execute("INSERT INTO recipe_tags VALUES (?, ?)", (rid, f"tag{i:02d}"))

    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    # 12 tags, capped at 8 -> 4 hidden, summarized by the disclosure
    assert "+4 more tags" in r.text
    assert 'class="tag-more"' in r.text
    # first tag is in the visible set; an overflow tag is still in the HTML
    assert "tag01" in r.text
    assert "tag12" in r.text


def test_recipe_detail_no_tag_disclosure_when_few(client: TestClient, seeded_db_path):
    """<=8 tags: no disclosure at all (Broccoli Stir Fry has two)."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert "more tags" not in r.text
    assert "tag-more" not in r.text


def test_recipe_detail_shows_delete_button(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert f'action="/recipes/{rid}/delete"' in r.text
    assert "Delete recipe" in r.text


def _set_nutrition(db_path, name: str, nutrition_json: str | None) -> int:
    """Set a recipe's nutrition_json column and return its id (test helper)."""
    with connect(db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "UPDATE recipes SET nutrition_json = ? WHERE id = ?", (nutrition_json, rid)
        )
    return rid


def test_recipe_detail_renders_nutrition(client: TestClient, seeded_db_path):
    """A recipe with a full macro dict renders every macro with its unit."""
    rid = _set_nutrition(
        seeded_db_path,
        "Broccoli Stir Fry",
        '{"calories": 210, "protein_g": 9, "fat_g": 7.5, '
        '"carbs_g": 28, "fiber_g": 4, "sodium_mg": 320}',
    )
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert "nutrition-section" in r.text
    for label in ("Calories", "Protein", "Carbs", "Fat", "Fiber", "Sodium"):
        assert label in r.text
    assert "per serving" in r.text
    # Whole numbers render without a trailing ".0"; fractional values are kept.
    assert "210" in r.text and "7.5" in r.text
    assert "kcal" in r.text and "mg" in r.text


def test_recipe_detail_nutrition_omits_missing_macros(client: TestClient, seeded_db_path):
    """Only the macros actually present render; absent/None ones are skipped."""
    rid = _set_nutrition(
        seeded_db_path,
        "Broccoli Stir Fry",
        '{"calories": 150, "protein_g": null, "fat_g": null, '
        '"carbs_g": null, "fiber_g": null, "sodium_mg": null}',
    )
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert "nutrition-section" in r.text
    assert "Calories" in r.text
    assert "Protein" not in r.text
    assert "Sodium" not in r.text


def test_recipe_detail_no_nutrition_section_when_absent(client: TestClient, seeded_db_path):
    """No nutrition column (the seed default) renders no panel."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert "nutrition-section" not in r.text


def test_recipe_detail_no_nutrition_section_when_all_empty(client: TestClient, seeded_db_path):
    """An all-null macro dict is treated as no data — no panel rendered."""
    rid = _set_nutrition(
        seeded_db_path,
        "Broccoli Stir Fry",
        '{"calories": null, "protein_g": null, "fat_g": null, '
        '"carbs_g": null, "fiber_g": null, "sodium_mg": null}',
    )
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert "nutrition-section" not in r.text


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


def test_pantry_search_suggestion_has_freshness_date(client: TestClient):
    from datetime import date, timedelta

    r = client.get("/pantry", params={"search": "broccoli"})
    assert r.status_code == 200
    expected = (date.today() + timedelta(days=5)).isoformat()
    assert expected in r.text


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
    # Dates are humanized for reading: "Week of Apr 20", not the raw ISO.
    assert "Week of Apr 20" in r.text
    assert "draft" in r.text

    r = client.get(f"/plans/{plan_id}")
    assert r.status_code == 200
    assert "Broccoli Stir Fry" in r.text
    # Day is title-cased in the row ("Mon"); meal slot stays lowercase.
    assert "Mon" in r.text and "dinner" in r.text


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


def test_plan_shopping_collapses_multi_recipe_provenance(client: TestClient, seeded_db_path):
    """An ingredient used by several recipes collapses to an 'in N recipes'
    disclosure; one used by a single recipe renders inline 'for <recipe>'."""
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
    # broccoli is in both seeded recipes -> collapsed disclosure, not a wall
    assert "in 2 recipes" in r.text
    assert "<details" in r.text and "<summary>in 2 recipes</summary>" in r.text
    # the single-recipe 'other ingredient' stays inline
    assert "for Broccoli Stir Fry" in r.text


def test_plan_shopping_buy_items_are_checkable(client: TestClient, seeded_db_path):
    """Need-to-buy rows render a checkbox so the list can be ticked off in store."""
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-05-04') RETURNING id"
        ).fetchone()["id"]
        rid = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Stir Fry'").fetchone()["id"]
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)",
            (plan_id, rid),
        )

    r = client.get(f"/plans/{plan_id}/shopping")
    assert r.status_code == 200
    # the 'other ingredient' is not in the pantry -> a checkable buy row
    assert 'class="shop-check"' in r.text
    assert 'type="checkbox"' in r.text


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
    """Regression: serve-web must apply any pending migrations before binding
    a port, so a stale DB never surfaces as a 500 at query time.

    v0.1.0 ships zero migration files. To exercise the bootstrap, this test
    redirects the migrations dir to a tmp_path containing a synthetic pending
    migration and confirms serve-web applies it.
    """
    from typer.testing import CliRunner

    from pantry_cooking_vibes import db as db_module
    from pantry_cooking_vibes.cli import app as cli_app
    from pantry_cooking_vibes.db import get_connection, init_db

    db = tmp_path / "app.db"
    init_db(db_path=db)

    fake_migrations = tmp_path / "migrations"
    fake_migrations.mkdir()
    (fake_migrations / "9001_synthetic_pending.sql").write_text(
        "CREATE TABLE IF NOT EXISTS synthetic_marker (id INTEGER PRIMARY KEY);"
    )

    # run_migrations() binds _MIGRATIONS_DIR at definition time via its default
    # arg, so patching the module constant doesn't affect existing calls. Wrap
    # the function so the cli's `from ... import run_migrations` (which does a
    # module-attribute lookup at call time) picks up the synthetic dir.
    from functools import partial

    monkeypatch.setattr(
        db_module,
        "run_migrations",
        partial(db_module.run_migrations, migrations_dir=fake_migrations),
    )

    # Stub uvicorn so serve-web runs its bootstrap without binding a port.
    invoked = {}

    def fake_run(*args, **kwargs):
        invoked["ran"] = True

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = CliRunner().invoke(cli_app, ["serve-web", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert invoked.get("ran") is True
    assert "Applied pending migrations" in result.output
    assert "9001_synthetic_pending.sql" in result.output

    with get_connection(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        recorded = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == 9001
    assert "9001_synthetic_pending.sql" in recorded
    assert "synthetic_marker" in tables


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


def test_post_plans_repeat_does_not_500(client: TestClient, seeded_db_path):
    """Clicking '+ New plan for this week' twice must not raise
    ``sqlite3.IntegrityError: UNIQUE constraint failed: meal_plans.week_of``.
    Second submission should redirect to the existing draft, not 500.
    """
    r1 = client.post("/plans", data={}, follow_redirects=False)
    assert r1.status_code == 303
    first_id = int(r1.headers["location"].split("/plans/")[1])

    r2 = client.post("/plans", data={}, follow_redirects=False)
    assert r2.status_code == 303
    second_id = int(r2.headers["location"].split("/plans/")[1])
    assert second_id == first_id

    with connect(seeded_db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM meal_plans WHERE status = 'draft'").fetchone()[0]
    assert count == 1


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


def test_post_recipe_add_to_plan_explicit_week_of(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]

    r = client.post(
        f"/recipes/{rid}/add-to-current-week",
        data={"week_of": "2026-06-07"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    plan_id = int(r.headers["location"].split("/plans/")[1])
    with connect(seeded_db_path) as conn:
        plan = conn.execute("SELECT week_of FROM meal_plans WHERE id = ?", (plan_id,)).fetchone()
    assert plan["week_of"] == "2026-06-07"


def test_post_recipe_add_to_plan_non_sunday_week_of_rejected(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]

    r = client.post(
        f"/recipes/{rid}/add-to-current-week",
        data={"week_of": "2026-06-08"},  # Monday
        follow_redirects=False,
    )
    assert r.status_code == 422


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


def test_plans_print_includes_per_recipe_ingredients_and_instructions(
    client: TestClient, seeded_db_path
):
    """Each recipe section must contain that recipe's own ingredients and
    instructions, not just the merged shopping list."""
    with connect(seeded_db_path) as conn:
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-05-04') RETURNING id"
        ).fetchone()["id"]
        for rid in conn.execute("SELECT id FROM recipes").fetchall():
            conn.execute(
                "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)",
                (plan_id, rid["id"]),
            )

    body = client.get(f"/plans/{plan_id}/print").text
    assert "2 cups broccoli florets" in body
    assert "1 head broccoli" in body
    assert "4 cups vegetable stock" in body
    assert "Stir fry broccoli" in body
    assert "Simmer broccoli in stock" in body
    # Servings reflect recipe yield (4 / 6), not the schema default of 1.
    assert "serves 4" in body
    assert "serves 6" in body


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
    assert r.headers["Referrer-Policy"] == "same-origin"


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


# ---------- LAN-IP same-origin regression ----------


@pytest.mark.parametrize(
    "host,origin",
    [
        # Plain LAN deploy (Docker / Portainer): browser at LAN IP+port.
        ("192.168.50.195:30058", "http://192.168.50.195:30058"),
        # Different port, same shape.
        ("192.168.1.10:8080", "http://192.168.1.10:8080"),
        # Hostname with port.
        ("homelab.lan:30055", "http://homelab.lan:30055"),
    ],
)
def test_lan_ip_post_with_matching_origin_allowed(client: TestClient, seeded_db_path, host, origin):
    """Same-origin POST from a LAN IP/host with a non-default port must pass."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/add-to-current-week",
        data={"redirect_to": f"/recipes/{rid}"},
        headers={
            "host": host,
            "origin": origin,
            "referer": f"{origin}/recipes/{rid}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, (
        f"got {r.status_code} {r.text!r} for host={host!r} origin={origin!r}"
    )


def test_lan_ip_post_with_referer_only_allowed(client: TestClient, seeded_db_path):
    """Some browsers strip Origin on same-origin POSTs. Referer fallback must
    also tolerate LAN IP + port."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/add-to-current-week",
        data={"redirect_to": f"/recipes/{rid}"},
        headers={
            "host": "192.168.50.195:30058",
            "referer": f"http://192.168.50.195:30058/recipes/{rid}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, f"got {r.status_code} {r.text!r}"


@pytest.mark.parametrize(
    "host,origin",
    [
        # Reverse proxy strips port from Origin (NPM, Traefik default-host, Pi-hole).
        ("192.168.50.195:30058", "http://192.168.50.195"),
        # Browser served at port 80 (proxy fronts container), submits POST.
        # Host preserved with port by proxy, Origin omits :80 per RFC.
        ("homelab.lan:30058", "http://homelab.lan"),
        # HTTPS proxy fronting HTTP container; Origin uses default :443.
        ("homelab.lan:30058", "https://homelab.lan"),
    ],
)
def test_post_with_proxy_stripped_port_in_origin_allowed(
    client: TestClient, seeded_db_path, host, origin
):
    """Reverse proxies (NPM, Pi-hole, Traefik) forward Host with the backend
    port while browser Origin omits the public default port per RFC 6454.
    Port mismatch alone must not block same-hostname POSTs."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/add-to-current-week",
        data={"redirect_to": f"/recipes/{rid}"},
        headers={"host": host, "origin": origin},
        follow_redirects=False,
    )
    assert r.status_code == 303, (
        f"got {r.status_code} {r.text!r} for host={host!r} origin={origin!r}"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://attacker.example",  # different hostname
        "http://attacker.example:30058",  # same port, attacker host
    ],
)
def test_post_blocked_when_origin_hostname_differs(client: TestClient, seeded_db_path, origin):
    """CSRF guard must still fire when the Origin hostname differs from Host —
    that's the actual browser-CSRF threat the middleware exists for."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/add-to-current-week",
        data={"redirect_to": f"/recipes/{rid}"},
        headers={"host": "192.168.50.195:30058", "origin": origin},
        follow_redirects=False,
    )
    assert r.status_code == 403, f"got {r.status_code} for origin={origin!r}"


def test_post_blocked_when_origin_null_and_cross_site(client: TestClient, seeded_db_path):
    """``Origin: null`` from an attacker-controlled context (sandboxed iframe,
    cross-origin redirect chain) MUST still be blocked. The browser tags those
    with ``Sec-Fetch-Site: cross-site``, which is a Forbidden header — JS
    cannot forge it — so it's the authoritative cross-origin signal."""
    with connect(seeded_db_path) as conn:
        rid = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()["id"]
    r = client.post(
        f"/recipes/{rid}/add-to-current-week",
        data={"redirect_to": f"/recipes/{rid}"},
        headers={
            "host": "192.168.50.195:30058",
            "origin": "null",
            "sec-fetch-site": "cross-site",
        },
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_allowed_for_real_chrome_same_origin_form_submit(client: TestClient, seeded_db_path):
    """Regression: replays the exact headers Chrome sends for a same-origin
    form POST when the response carries ``Referrer-Policy: no-referrer``.
    Chrome strips Referer and sets ``Origin: null``, but tags the request
    ``Sec-Fetch-Site: same-origin`` because the navigation IS same-origin.

    These are the literal headers captured from a failing
    ``POST http://127.0.0.1:8000/plans`` against Chrome 148 / macOS:

        Host: 127.0.0.1:8000
        Origin: null
        Sec-Fetch-Site: same-origin
        Sec-Fetch-Mode: navigate
        Sec-Fetch-Dest: document
        Sec-Fetch-User: ?1
        (no Referer)

    The CSRF guard MUST trust ``Sec-Fetch-Site`` (browser-only, unforgeable)
    and let this through, otherwise the New Plan button 403s for real users.
    """
    r = client.post(
        "/plans",
        data={},
        headers={
            "host": "127.0.0.1:8000",
            "origin": "null",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-user": "?1",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, (
        f"expected 303 for same-origin Chrome POST, got {r.status_code}: {r.text!r}"
    )


def test_referrer_policy_does_not_strip_origin_on_same_origin_posts():
    """Root-cause guard: ``Referrer-Policy: no-referrer`` is what triggered
    Chrome to send ``Origin: null`` on a legitimate same-origin POST. The
    middleware should now use a policy (``same-origin`` or stricter on the
    cross-origin axis) that preserves Origin within the same origin."""
    from pantry_cooking_vibes.web.app import create_app

    app = create_app()
    test_client = TestClient(app)
    r = test_client.get("/healthz")
    assert r.status_code == 200
    policy = r.headers.get("Referrer-Policy", "")
    assert policy != "no-referrer", (
        "no-referrer causes Chrome to null the Origin header on same-origin "
        "form POSTs — use same-origin or strict-origin-when-cross-origin"
    )


# ---------- recipe edit (issue #49) ----------


def test_get_recipe_edit_form_prefilled(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")
    r = client.get(f"/recipes/{rid}/edit")
    assert r.status_code == 200
    body = r.text
    assert f'action="/recipes/{rid}/edit"' in body
    assert 'value="Broccoli Stir Fry"' in body
    assert 'value="25"' in body  # cooking_time_min
    assert "2 cups broccoli florets" in body  # ingredients textarea
    assert "Stir fry broccoli in a hot pan." in body  # steps textarea
    assert "asian, quick" in body  # tags, comma-joined
    # The mapping-loss warning must be the visible note, not a faint hint.
    assert 'class="field-note"' in body


def test_get_recipe_edit_form_missing_404(client: TestClient):
    assert client.get("/recipes/99999/edit").status_code == 404


def test_recipe_detail_links_to_edit(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")
    r = client.get(f"/recipes/{rid}")
    assert r.status_code == 200
    assert f'href="/recipes/{rid}/edit"' in r.text


def test_post_recipe_edit_updates_and_redirects(client: TestClient, seeded_db_path):
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")

    r = client.post(
        f"/recipes/{rid}/edit",
        data={
            "name": "Broccoli Mega Fry",
            "cooking_time_min": "15",
            "servings": "2",
            "image_url": "https://example.com/pic.jpg",
            "tags": "Quick, weeknight",
            "ingredients": "2 cups broccoli florets\n1 tbsp soy sauce",
            "instructions": "Chop everything.\nFry it.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/recipes/{rid}?saved=1"

    # Following the redirect flashes a save confirmation; a plain visit doesn't.
    flashed = client.get(f"/recipes/{rid}?saved=1").text
    assert "Recipe saved" in flashed
    detail = client.get(f"/recipes/{rid}").text
    assert "Recipe saved" not in detail
    assert "Broccoli Mega Fry" in detail
    assert "1 tbsp soy sauce" in detail
    assert "Fry it." in detail
    with connect(seeded_db_path) as conn:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
        assert row["name"] == "Broccoli Mega Fry"
        assert row["cooking_time_min"] == 15
        assert row["image_url"] == "https://example.com/pic.jpg"
        tags = {
            t["tag"]
            for t in conn.execute(
                "SELECT tag FROM recipe_tags WHERE recipe_id = ?", (rid,)
            ).fetchall()
        }
        assert tags == {"quick", "weeknight"}
        # The unchanged ingredient line kept its canonical mapping.
        kept = conn.execute(
            "SELECT canonical_id FROM recipe_ingredients "
            "WHERE recipe_id = ? AND original_text = '2 cups broccoli florets'",
            (rid,),
        ).fetchone()
        assert kept["canonical_id"] is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},  # name required
        {"image_url": "javascript:alert(1)"},  # XSS-capable scheme rejected
        {"cooking_time_min": "abc"},  # non-numeric
        {"servings": "0"},  # below minimum
    ],
)
def test_post_recipe_edit_invalid_keeps_recipe_and_rerenders(
    client: TestClient, seeded_db_path, overrides
):
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")
        before = dict(conn.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone())

    data = {
        "name": "Renamed",
        "cooking_time_min": "10",
        "servings": "2",
        "image_url": "",
        "tags": "quick",
        "ingredients": "1 thing",
        "instructions": "Do it.",
    }
    data.update(overrides)
    r = client.post(f"/recipes/{rid}/edit", data=data, follow_redirects=False)
    assert r.status_code == 422
    assert 'class="form-error"' in r.text

    with connect(seeded_db_path) as conn:
        after = dict(conn.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone())
    assert after == before  # rejected edit leaves the recipe untouched


def test_post_recipe_edit_missing_404(client: TestClient):
    r = client.post("/recipes/99999/edit", data={"name": "x"}, follow_redirects=False)
    assert r.status_code == 404


def test_recipe_edit_xss_payloads_are_escaped_everywhere(client: TestClient, seeded_db_path):
    """A recipe whose fields contain markup must render inert on the detail
    page AND inside the edit form's value/textarea echoes (no raw <script>)."""
    with connect(seeded_db_path) as conn:
        rid = _recipe_id(conn, "Broccoli Stir Fry")

    payload = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    r = client.post(
        f"/recipes/{rid}/edit",
        data={
            "name": f"Evil {payload}",
            "cooking_time_min": "",
            "servings": "",
            "image_url": "",
            "tags": payload,
            "ingredients": payload,
            "instructions": payload,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    for page in (client.get(f"/recipes/{rid}").text, client.get(f"/recipes/{rid}/edit").text):
        assert "<script>alert(1)</script>" not in page
        assert "<img src=x onerror" not in page
        assert "&lt;script&gt;" in page  # escaped, not dropped
