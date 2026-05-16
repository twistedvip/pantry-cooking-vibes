"""Tests for Phase 6 MCP tools (pure functions; the server is a thin wrapper)."""

from __future__ import annotations

import asyncio

import pytest

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.mcp_server import tools

# ---------- search_recipes ----------


def test_search_recipes_fts_match(seeded_db_path):
    rows = tools.search_recipes(query="broccoli", db_path=seeded_db_path)
    names = [r["name"] for r in rows]
    assert any("Broccoli" in n for n in names)


# Regression: bare FTS5 input that contains operator characters (-, :, *, ", ())
# used to crash the route with sqlite3.OperationalError ("no such column: Pan"
# from "One-Pan Chicken Parm on Veggies"). Each token is now phrase-quoted so
# operators are neutralized.
@pytest.mark.parametrize(
    "query",
    [
        "One-Pan Chicken Parm on Veggies",
        "Pan:Roast",
        "chicken*",
        'broccoli "stir fry"',
        "(soup) - bisque",
        "name:foo column:bar",
        "broccoli-soup",
        "  ",
    ],
)
def test_search_recipes_fts_operator_chars_do_not_crash(seeded_db_path, query):
    # Must not raise; result list may be empty.
    rows = tools.search_recipes(query=query, db_path=seeded_db_path)
    assert isinstance(rows, list)


def test_search_recipes_hyphenated_token_still_matches(seeded_db_path):
    # "broccoli-soup" should still find Broccoli Soup since FTS5 tokenizer
    # splits on the hyphen inside a phrase-quoted token.
    rows = tools.search_recipes(query="broccoli-soup", db_path=seeded_db_path)
    names = [r["name"] for r in rows]
    assert any("Broccoli Soup" in n for n in names)


def test_fts5_escape_query_phrases_each_token():
    from pantry_cooking_vibes.mcp_server.tools import _fts5_escape_query

    assert _fts5_escape_query("One-Pan Chicken") == '"One-Pan" "Chicken"'
    assert _fts5_escape_query("") == ""
    assert _fts5_escape_query("   ") == ""
    # Embedded double quotes get escaped by doubling per FTS5 spec.
    assert _fts5_escape_query('say "hi"') == '"say" """hi"""'


def test_search_recipes_empty_query_orders_by_rating(seeded_db_path):
    rows = tools.search_recipes(db_path=seeded_db_path)
    assert len(rows) >= 2
    ratings = [r["rating"] for r in rows if r["rating"] is not None]
    assert ratings == sorted(ratings, reverse=True)


def test_search_recipes_max_time_filter(seeded_db_path):
    rows = tools.search_recipes(max_time_min=30, db_path=seeded_db_path)
    assert rows
    for r in rows:
        assert r["cooking_time_min"] is not None and r["cooking_time_min"] <= 30


def test_search_recipes_tags_require_all(seeded_db_path):
    soup_only = tools.search_recipes(tags=["soup"], db_path=seeded_db_path)
    assert len(soup_only) == 1 and soup_only[0]["name"] == "Broccoli Soup"

    quick_asian = tools.search_recipes(tags=["quick", "asian"], db_path=seeded_db_path)
    assert len(quick_asian) == 1

    none = tools.search_recipes(tags=["quick", "soup"], db_path=seeded_db_path)
    assert none == []


def test_search_recipes_tags_case_insensitive(seeded_db_path):
    rows = tools.search_recipes(tags=["QUICK"], db_path=seeded_db_path)
    assert len(rows) == 1


def test_search_recipes_limit(seeded_db_path):
    assert len(tools.search_recipes(limit=1, db_path=seeded_db_path)) == 1


def test_search_recipes_limit_clamps_above_cap(seeded_db_path):
    # 9999 should clamp to MAX_RESULT_LIMIT — should not raise
    rows = tools.search_recipes(limit=9999, db_path=seeded_db_path)
    assert len(rows) >= 1


def test_search_recipes_limit_zero_returns_one(seeded_db_path):
    # _clamp_limit floors at 1 to avoid silent zero-result surprises
    rows = tools.search_recipes(limit=0, db_path=seeded_db_path)
    assert len(rows) == 1


def test_search_recipes_sources_filter(seeded_db_path):
    # Seeded fixture has one 'manual' and one 'url' recipe.
    manual_only = tools.search_recipes(sources=["manual"], db_path=seeded_db_path)
    assert {r["source"] for r in manual_only} == {"manual"}

    url_only = tools.search_recipes(sources=["url"], db_path=seeded_db_path)
    assert {r["source"] for r in url_only} == {"url"}

    both = tools.search_recipes(sources=["manual", "url"], db_path=seeded_db_path)
    assert {r["source"] for r in both} == {"manual", "url"}

    # None / empty list = no source restriction.
    unrestricted_none = tools.search_recipes(sources=None, db_path=seeded_db_path)
    unrestricted_empty = tools.search_recipes(sources=[], db_path=seeded_db_path)
    assert len(unrestricted_none) == len(unrestricted_empty) >= 2


def test_list_recipe_sources(seeded_db_path):
    assert tools.list_recipe_sources(db_path=seeded_db_path) == ["manual", "url"]


# ---------- search_recipes by ingredient ----------


def test_search_recipes_by_ingredient_and_includes_matching(seeded_db_path):
    """Both seeded recipes contain broccoli; AND-filter on broccoli returns both."""
    rows = tools.search_recipes(
        ingredients=["broccoli"], ingredient_mode="and", db_path=seeded_db_path
    )
    names = {r["name"] for r in rows}
    assert "Broccoli Stir Fry" in names
    assert "Broccoli Soup" in names


def test_search_recipes_by_ingredient_unknown_returns_empty(seeded_db_path):
    rows = tools.search_recipes(ingredients=["zzznotacanonicalzzz"], db_path=seeded_db_path)
    assert rows == []


def test_search_recipes_by_ingredient_substring_match(seeded_db_path):
    """A partial term matches canonical names by substring (canonical names are
    specific, e.g. 'chicken breast'; users type 'chicken'). 'brocc' resolves to
    the 'broccoli' canonical and returns both seeded recipes."""
    rows = tools.search_recipes(ingredients=["brocc"], db_path=seeded_db_path)
    names = {r["name"] for r in rows}
    assert names == {"Broccoli Stir Fry", "Broccoli Soup"}


def test_search_recipes_by_ingredient_and_requires_all(seeded_db_path):
    """Only Stir Fry has the 'other' canonical (the second seeded ingredient)."""
    with connect(seeded_db_path) as conn:
        other = conn.execute(
            "SELECT name FROM canonical_ingredients WHERE name != 'broccoli' ORDER BY id LIMIT 1"
        ).fetchone()["name"]

    rows = tools.search_recipes(
        ingredients=["broccoli", other], ingredient_mode="and", db_path=seeded_db_path
    )
    names = {r["name"] for r in rows}
    assert names == {"Broccoli Stir Fry"}


def test_search_recipes_by_ingredient_or_unions(seeded_db_path):
    """OR mode: broccoli matches both recipes, unknown is ignored."""
    rows = tools.search_recipes(
        ingredients=["broccoli"], ingredient_mode="or", db_path=seeded_db_path
    )
    assert len(rows) == 2


def test_search_recipes_invalid_ingredient_mode_raises(seeded_db_path):
    with pytest.raises(ValueError):
        tools.search_recipes(
            ingredients=["broccoli"], ingredient_mode="xor", db_path=seeded_db_path
        )


def test_search_recipes_pantry_only_excludes_recipes_with_unmet_ingredients(
    seeded_db_path,
):
    """Stir Fry needs broccoli + 'other'. Only broccoli is in pantry, so Stir Fry
    is excluded. Soup has broccoli + an unmapped ingredient, which counts as
    pantry-coverable (unmapped ignored), so Soup qualifies."""
    rows = tools.search_recipes(pantry_only=True, db_path=seeded_db_path)
    names = {r["name"] for r in rows}
    assert "Broccoli Soup" in names
    assert "Broccoli Stir Fry" not in names


def test_search_recipes_pantry_only_with_ingredients_filter(seeded_db_path):
    """Combining ingredient filter + pantry_only returns only Soup (broccoli AND in pantry)."""
    rows = tools.search_recipes(ingredients=["broccoli"], pantry_only=True, db_path=seeded_db_path)
    names = {r["name"] for r in rows}
    assert names == {"Broccoli Soup"}


# ---------- get_recipe ----------


def test_get_recipe_full_hydration(seeded_db_path):
    row = tools.search_recipes(query="stir fry", db_path=seeded_db_path)[0]
    rec = tools.get_recipe(row["id"], db_path=seeded_db_path)
    assert rec is not None
    assert "Stir Fry" in rec["name"]
    assert len(rec["ingredients"]) == 2
    # canonical name must be joined for Claude's reasoning
    assert all("canonical_name" in i for i in rec["ingredients"])
    assert "quick" in rec["tags"] and "asian" in rec["tags"]


def test_get_recipe_missing_returns_none(seeded_db_path):
    assert tools.get_recipe(99999, db_path=seeded_db_path) is None


# ---------- delete_recipe ----------


def test_delete_recipe_cascades_and_keeps_fts_synced(seeded_db_path):
    """Deleting the row must cascade to children and remove it from FTS."""
    with connect(seeded_db_path) as conn:
        recipe_id = conn.execute(
            "SELECT id FROM recipes WHERE name = 'Broccoli Stir Fry'"
        ).fetchone()["id"]
        # Add a favorite + a meal plan item so we exercise both cascades.
        conn.execute("INSERT INTO recipe_favorites (recipe_id) VALUES (?)", (recipe_id,))
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-01-01') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)",
            (plan_id, recipe_id),
        )

    tools.delete_recipe(recipe_id, db_path=seeded_db_path)

    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is None
        # All four cascading children gone.
        for table in ("recipe_ingredients", "recipe_tags", "recipe_favorites", "meal_plan_items"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE recipe_id = ?", (recipe_id,)
            ).fetchone()[0]
            assert n == 0, f"{table} should cascade-delete"
        # FTS index reflects the delete (recipes_ad trigger fired).
        fts_hits = conn.execute(
            "SELECT 1 FROM recipes_fts WHERE recipes_fts MATCH 'Stir Fry'"
        ).fetchall()
        assert fts_hits == []
        # Sibling recipe is untouched.
        assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 1


def test_delete_recipe_missing_raises(seeded_db_path):
    with pytest.raises(ValueError, match="not found"):
        tools.delete_recipe(99999, db_path=seeded_db_path)


# ---------- delete_meal_plan ----------


def test_delete_meal_plan_cascades_items_and_favorites(seeded_db_path):
    with connect(seeded_db_path) as conn:
        recipe_id = conn.execute(
            "SELECT id FROM recipes WHERE name = 'Broccoli Stir Fry'"
        ).fetchone()["id"]
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-02-01') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id) VALUES (?, ?)",
            (plan_id, recipe_id),
        )
        conn.execute("INSERT INTO meal_plan_favorites (plan_id) VALUES (?)", (plan_id,))
        broccoli_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name = 'broccoli'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO shopping_list_items (plan_id, canonical_id, quantity_needed) "
            "VALUES (?, ?, 1)",
            (plan_id, broccoli_id),
        )

    tools.delete_meal_plan(plan_id, db_path=seeded_db_path)

    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT 1 FROM meal_plans WHERE id = ?", (plan_id,)).fetchone() is None
        for table in ("meal_plan_items", "meal_plan_favorites", "shopping_list_items"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?", (plan_id,)
            ).fetchone()[0]
            assert n == 0, f"{table} should cascade-delete"
        # Recipe survives.
        assert conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone()


def test_delete_meal_plan_missing_raises(seeded_db_path):
    with pytest.raises(ValueError, match="not found"):
        tools.delete_meal_plan(99999, db_path=seeded_db_path)


# ---------- pantry ----------


def test_list_pantry_returns_canonical_name(seeded_db_path):
    items = tools.list_pantry(db_path=seeded_db_path)
    assert len(items) == 1
    assert items[0]["canonical_name"] == "broccoli"
    assert items[0]["quantity"] == 2.0


def test_add_and_remove_pantry_item(seeded_db_path):
    with connect(seeded_db_path) as conn:
        cid = conn.execute(
            "SELECT id FROM canonical_ingredients ORDER BY id LIMIT 1 OFFSET 5"
        ).fetchone()[0]

    new = tools.add_pantry_item(cid, quantity=3.0, unit="lb", db_path=seeded_db_path)
    assert new["id"] > 0 and new["quantity"] == 3.0

    assert len(tools.list_pantry(db_path=seeded_db_path)) == 2

    res = tools.remove_pantry_item(new["id"], db_path=seeded_db_path)
    assert res["removed"] is True
    assert tools.remove_pantry_item(new["id"], db_path=seeded_db_path)["removed"] is False
    assert len(tools.list_pantry(db_path=seeded_db_path)) == 1


def test_add_pantry_negative_quantity_raises(seeded_db_path):
    with pytest.raises(ValueError):
        tools.add_pantry_item(1, quantity=-1.0, db_path=seeded_db_path)


def test_update_pantry_item_changes_quantity_and_unit(seeded_db_path):
    item = tools.list_pantry(db_path=seeded_db_path)[0]
    updated = tools.update_pantry_item(item["id"], quantity=4.5, unit="lb", db_path=seeded_db_path)
    assert updated["quantity"] == 4.5
    assert updated["unit"] == "lb"

    refetched = tools.list_pantry(db_path=seeded_db_path)[0]
    assert refetched["quantity"] == 4.5
    assert refetched["unit"] == "lb"


def test_update_pantry_item_missing_raises(seeded_db_path):
    with pytest.raises(ValueError, match="not found"):
        tools.update_pantry_item(99999, quantity=1.0, db_path=seeded_db_path)


def test_update_pantry_item_negative_quantity_raises(seeded_db_path):
    item = tools.list_pantry(db_path=seeded_db_path)[0]
    with pytest.raises(ValueError):
        tools.update_pantry_item(item["id"], quantity=-1.0, db_path=seeded_db_path)


# ---------- find_canonical_ingredient ----------


def test_find_canonical_exact_match_first(seeded_db_path):
    rows = tools.find_canonical_ingredient("broccoli", db_path=seeded_db_path)
    assert rows
    assert rows[0]["name"] == "broccoli"


def test_find_canonical_substring_works(seeded_db_path):
    rows = tools.find_canonical_ingredient("brocc", db_path=seeded_db_path)
    assert any(r["name"] == "broccoli" for r in rows)


def test_find_canonical_empty_query_returns_empty(seeded_db_path):
    assert tools.find_canonical_ingredient("", db_path=seeded_db_path) == []
    assert tools.find_canonical_ingredient("   ", db_path=seeded_db_path) == []


def test_find_canonical_no_match(seeded_db_path):
    assert tools.find_canonical_ingredient("zzzzzqxxxnomatch", db_path=seeded_db_path) == []


def test_find_canonical_includes_freshness_days(seeded_db_path):
    rows = tools.find_canonical_ingredient("broccoli", db_path=seeded_db_path)
    assert rows
    assert "freshness_days" in rows[0]
    assert rows[0]["freshness_days"] == 5  # broccoli: 3-5 days fridge


# ---------- meal plan ----------


def test_create_meal_plan_invalid_week(seeded_db_path):
    with pytest.raises(ValueError):
        tools.create_meal_plan("not-a-date", db_path=seeded_db_path)


@pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01", "2026-99-99"])
def test_create_meal_plan_rejects_impossible_dates(seeded_db_path, bad):
    """Regex format alone isn't enough; impossible calendar dates must also be rejected."""
    with pytest.raises(ValueError):
        tools.create_meal_plan(bad, db_path=seeded_db_path)


def test_create_meal_plan_duplicate_week_returns_existing_draft(seeded_db_path):
    """Reproduces the IntegrityError seen when creating a second draft for an
    already-drafted week. The partial unique index ``idx_meal_plans_week_draft``
    permits only one draft per ``week_of``; ``create_meal_plan`` must be
    idempotent on that constraint instead of raising ``sqlite3.IntegrityError``.
    """
    first = tools.create_meal_plan("2026-05-03", notes="first", db_path=seeded_db_path)
    second = tools.create_meal_plan("2026-05-03", notes="ignored", db_path=seeded_db_path)
    assert second["id"] == first["id"]
    assert second["status"] == "draft"
    assert second["notes"] == "first"

    with connect(seeded_db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM meal_plans WHERE week_of = ? AND status = 'draft'",
            ("2026-05-03",),
        ).fetchone()[0]
    assert count == 1


def test_create_meal_plan_after_confirm_allows_new_draft(seeded_db_path):
    """Partial index only forbids duplicate *drafts*; once the prior plan is
    confirmed, a new draft for the same week should succeed."""
    first = tools.create_meal_plan("2026-05-03", db_path=seeded_db_path)
    with connect(seeded_db_path) as conn:
        conn.execute("UPDATE meal_plans SET status = 'confirmed' WHERE id = ?", (first["id"],))
    second = tools.create_meal_plan("2026-05-03", db_path=seeded_db_path)
    assert second["id"] != first["id"]
    assert second["status"] == "draft"


def test_create_meal_plan_basic(seeded_db_path):
    plan = tools.create_meal_plan("2026-05-04", notes="test", db_path=seeded_db_path)
    assert plan["status"] == "draft"
    assert plan["notes"] == "test"

    fetched = tools.get_meal_plan(plan["id"], db_path=seeded_db_path)
    assert fetched is not None and fetched["items"] == []


def test_get_meal_plan_missing(seeded_db_path):
    assert tools.get_meal_plan(99999, db_path=seeded_db_path) is None


def test_add_and_remove_recipe_in_plan(seeded_db_path):
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]

    item = tools.add_recipe_to_plan(
        plan["id"],
        recipe["id"],
        day="mon",
        meal_slot="dinner",
        servings_planned=2,
        db_path=seeded_db_path,
    )
    assert item["day"] == "mon" and item["servings_planned"] == 2

    detail = tools.get_meal_plan(plan["id"], db_path=seeded_db_path)
    assert detail is not None
    assert len(detail["items"]) == 1
    assert "recipe_name" in detail["items"][0]

    assert tools.remove_meal_plan_item(item["id"], db_path=seeded_db_path)["removed"] is True
    after = tools.get_meal_plan(plan["id"], db_path=seeded_db_path)
    assert after is not None and after["items"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"day": "funday"},
        {"meal_slot": "brunch"},
        {"servings_planned": 0},
    ],
)
def test_add_recipe_validations(seeded_db_path, kwargs):
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    with pytest.raises(ValueError):
        tools.add_recipe_to_plan(plan["id"], recipe["id"], db_path=seeded_db_path, **kwargs)


def test_list_meal_plans_with_counts(seeded_db_path):
    p1 = tools.create_meal_plan("2026-04-13", db_path=seeded_db_path)
    p2 = tools.create_meal_plan("2026-04-20", db_path=seeded_db_path)
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    tools.add_recipe_to_plan(p2["id"], recipe["id"], db_path=seeded_db_path)

    plans = tools.list_meal_plans(db_path=seeded_db_path)
    assert plans[0]["week_of"] == "2026-04-20"  # newest first
    counts = {p["id"]: p["item_count"] for p in plans}
    assert counts[p1["id"]] == 0
    assert counts[p2["id"]] == 1


# ---------- shopping list ----------


def test_compute_shopping_list_categorizes(seeded_db_path):
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    for r in tools.search_recipes(db_path=seeded_db_path):
        tools.add_recipe_to_plan(plan["id"], r["id"], db_path=seeded_db_path)

    sl = tools.compute_shopping_list(plan["id"], db_path=seeded_db_path)

    covered_names = [c["name"] for c in sl["covered_by_pantry"]]
    assert "broccoli" in covered_names

    needed_names = [n["name"] for n in sl["needed"]]
    assert "broccoli" not in needed_names
    assert any(n != "broccoli" for n in needed_names)  # the "other" canonical

    uncategorized_text = [u["original_text"] for u in sl["uncategorized"]]
    assert any("stock" in t for t in uncategorized_text)


def test_compute_shopping_list_in_recipes_aggregates(seeded_db_path):
    """A canonical used by 2 recipes lists both recipe names in 'in_recipes'."""
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    for r in tools.search_recipes(db_path=seeded_db_path):
        tools.add_recipe_to_plan(plan["id"], r["id"], db_path=seeded_db_path)

    sl = tools.compute_shopping_list(plan["id"], db_path=seeded_db_path)
    broccoli = next(c for c in sl["covered_by_pantry"] if c["name"] == "broccoli")
    assert len(broccoli["in_recipes"]) == 2


def test_compute_shopping_list_empty_plan(seeded_db_path):
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    sl = tools.compute_shopping_list(plan["id"], db_path=seeded_db_path)
    assert sl["needed"] == [] and sl["covered_by_pantry"] == [] and sl["uncategorized"] == []


def test_compute_shopping_list_missing_plan_raises(seeded_db_path):
    with pytest.raises(ValueError):
        tools.compute_shopping_list(99999, db_path=seeded_db_path)


# ---------- server smoke ----------


def test_build_server_registers_all_tools():
    from pantry_cooking_vibes.mcp_server.server import build_server

    s = build_server()
    listed = asyncio.run(s.list_tools())
    names = {t.name for t in listed}
    expected = {
        "search_recipes",
        "get_recipe",
        "list_pantry",
        "add_pantry_item",
        "remove_pantry_item",
        "find_canonical_ingredient",
        "create_meal_plan",
        "add_recipe_to_plan",
        "remove_meal_plan_item",
        "list_meal_plans",
        "get_meal_plan",
        "compute_shopping_list",
    }
    assert expected == names


# ---------- AC12: user-only tools NOT in MCP server ----------


def test_user_only_tools_not_in_mcp_server():
    """set_recipe_favorite and set_meal_plan_favorite must NOT be MCP-exposed."""
    from pantry_cooking_vibes.mcp_server.server import build_server

    s = build_server()
    listed = asyncio.run(s.list_tools())
    names = {t.name for t in listed}
    assert "set_recipe_favorite" not in names
    assert "set_meal_plan_favorite" not in names


# ---------- add_to_current_week_plan ----------


def test_add_to_current_week_plan_creates_draft(seeded_db_path):
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    result = tools.add_to_current_week_plan(recipe["id"], db_path=seeded_db_path)
    assert "plan_id" in result and "item" in result
    item = result["item"]
    assert item["recipe_id"] == recipe["id"]
    assert item["day"] is None and item["meal_slot"] is None
    assert item["servings_planned"] == 1

    plan = tools.get_meal_plan(result["plan_id"], db_path=seeded_db_path)
    assert plan is not None
    assert plan["status"] == "draft"
    from pantry_cooking_vibes.dates import current_sunday

    assert plan["week_of"] == current_sunday().isoformat()


def test_add_to_current_week_plan_reuses_existing_draft(seeded_db_path):
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    r1 = tools.add_to_current_week_plan(recipe["id"], db_path=seeded_db_path)
    r2 = tools.add_to_current_week_plan(recipe["id"], db_path=seeded_db_path)
    assert r1["plan_id"] == r2["plan_id"]


def test_add_to_current_week_plan_duplicates(seeded_db_path):
    """Adding same recipe twice creates two distinct meal_plan_items rows."""
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    r1 = tools.add_to_current_week_plan(recipe["id"], db_path=seeded_db_path)
    r2 = tools.add_to_current_week_plan(recipe["id"], db_path=seeded_db_path)
    assert r1["plan_id"] == r2["plan_id"]
    assert r1["item"]["id"] != r2["item"]["id"]

    plan = tools.get_meal_plan(r1["plan_id"], db_path=seeded_db_path)
    assert plan is not None
    matching = [i for i in plan["items"] if i["recipe_id"] == recipe["id"]]
    assert len(matching) == 2


def test_add_to_current_week_plan_missing_recipe_raises(seeded_db_path):
    with pytest.raises(ValueError, match="not found"):
        tools.add_to_current_week_plan(99999, db_path=seeded_db_path)


def test_add_to_current_week_plan_explicit_week_of(seeded_db_path):
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    week_of = "2026-06-07"  # a known Sunday
    result = tools.add_to_current_week_plan(recipe["id"], week_of=week_of, db_path=seeded_db_path)
    plan = tools.get_meal_plan(result["plan_id"], db_path=seeded_db_path)
    assert plan is not None
    assert plan["week_of"] == week_of


def test_add_to_current_week_plan_explicit_week_of_reuses_draft(seeded_db_path):
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    week_of = "2026-06-07"
    r1 = tools.add_to_current_week_plan(recipe["id"], week_of=week_of, db_path=seeded_db_path)
    r2 = tools.add_to_current_week_plan(recipe["id"], week_of=week_of, db_path=seeded_db_path)
    assert r1["plan_id"] == r2["plan_id"]


def test_add_to_current_week_plan_invalid_week_of_raises(seeded_db_path):
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    with pytest.raises(ValueError):
        tools.add_to_current_week_plan(recipe["id"], week_of="not-a-date", db_path=seeded_db_path)


# ---------- set_meal_plan_favorite ----------


def test_set_meal_plan_favorite_toggle(seeded_db_path):
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    result = tools.set_meal_plan_favorite(plan["id"], True, db_path=seeded_db_path)
    assert result["is_favorite"] is True

    fetched = tools.get_meal_plan(plan["id"], db_path=seeded_db_path)
    assert fetched is not None
    assert fetched["is_favorite"] is True

    result = tools.set_meal_plan_favorite(plan["id"], False, db_path=seeded_db_path)
    assert result["is_favorite"] is False

    fetched = tools.get_meal_plan(plan["id"], db_path=seeded_db_path)
    assert fetched is not None
    assert fetched["is_favorite"] is False


def test_list_meal_plans_orders_favorites_first(seeded_db_path):
    p1 = tools.create_meal_plan("2026-04-06", db_path=seeded_db_path)
    tools.create_meal_plan("2026-04-13", db_path=seeded_db_path)
    tools.create_meal_plan("2026-04-20", db_path=seeded_db_path)

    tools.set_meal_plan_favorite(p1["id"], True, db_path=seeded_db_path)

    plans = tools.list_meal_plans(db_path=seeded_db_path)
    ids = [p["id"] for p in plans]
    assert ids[0] == p1["id"]  # favorite first despite oldest week_of


# ---------- clone_meal_plan ----------


def test_clone_meal_plan_basic(seeded_db_path):
    plan = tools.create_meal_plan("2026-04-13", notes="original", db_path=seeded_db_path)
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    tools.add_recipe_to_plan(plan["id"], recipe["id"], day="mon", db_path=seeded_db_path)

    cloned = tools.clone_meal_plan(plan["id"], db_path=seeded_db_path)
    assert cloned["id"] != plan["id"]
    assert cloned["status"] == "draft"
    assert cloned["notes"].startswith(f"Cloned from #{plan['id']}.")
    assert cloned["item_count"] == 1

    from datetime import date

    cloned_date = date.fromisoformat(cloned["week_of"])
    assert cloned_date.weekday() == 6  # must be a Sunday


def test_clone_meal_plan_empty(seeded_db_path):
    plan = tools.create_meal_plan("2026-04-13", db_path=seeded_db_path)
    cloned = tools.clone_meal_plan(plan["id"], db_path=seeded_db_path)
    assert cloned["item_count"] == 0


def test_clone_meal_plan_missing_raises(seeded_db_path):
    with pytest.raises(ValueError, match="not found"):
        tools.clone_meal_plan(99999, db_path=seeded_db_path)


# ---------- compute_pantry_coverage ----------


def test_compute_pantry_coverage_empty_plan(seeded_db_path):
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    cov = tools.compute_pantry_coverage(plan["id"], db_path=seeded_db_path)
    assert cov == {"plan_id": plan["id"], "covered": 0, "total": 0, "percent": 0, "missing": []}


def test_compute_pantry_coverage_all_covered(seeded_db_path):
    """A plan with only broccoli-based recipes; broccoli is in pantry -> 100%."""
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    with connect(seeded_db_path) as conn:
        soup_id = conn.execute("SELECT id FROM recipes WHERE name='Broccoli Soup'").fetchone()["id"]
    tools.add_recipe_to_plan(plan["id"], soup_id, db_path=seeded_db_path)
    cov = tools.compute_pantry_coverage(plan["id"], db_path=seeded_db_path)
    assert cov["percent"] == 100
    assert cov["covered"] == cov["total"]


def test_compute_pantry_coverage_none_covered(seeded_db_path):
    """Stir fry needs broccoli + other. Remove broccoli from pantry -> 0%."""
    with connect(seeded_db_path) as conn:
        conn.execute("DELETE FROM pantry")
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    with connect(seeded_db_path) as conn:
        stir_fry_id = conn.execute(
            "SELECT id FROM recipes WHERE name='Broccoli Stir Fry'"
        ).fetchone()["id"]
    tools.add_recipe_to_plan(plan["id"], stir_fry_id, db_path=seeded_db_path)
    cov = tools.compute_pantry_coverage(plan["id"], db_path=seeded_db_path)
    assert cov["percent"] == 0
    assert cov["covered"] == 0
    assert cov["total"] == 2
    assert len(cov["missing"]) == 2


def test_compute_pantry_coverage_mixed(seeded_db_path):
    """Stir fry has broccoli (in pantry) + other (not in pantry) -> 50%."""
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    with connect(seeded_db_path) as conn:
        stir_fry_id = conn.execute(
            "SELECT id FROM recipes WHERE name='Broccoli Stir Fry'"
        ).fetchone()["id"]
    tools.add_recipe_to_plan(plan["id"], stir_fry_id, db_path=seeded_db_path)
    cov = tools.compute_pantry_coverage(plan["id"], db_path=seeded_db_path)
    assert cov["total"] == 2
    assert cov["covered"] == 1
    assert cov["percent"] == 50


# ---------- remove_meal_plan_item_from_plan ----------


def test_remove_meal_plan_item_from_plan_match(seeded_db_path):
    plan = tools.create_meal_plan("2026-05-04", db_path=seeded_db_path)
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    item = tools.add_recipe_to_plan(plan["id"], recipe["id"], db_path=seeded_db_path)
    result = tools.remove_meal_plan_item_from_plan(plan["id"], item["id"], db_path=seeded_db_path)
    assert result["removed"] is True


def test_remove_meal_plan_item_from_plan_mismatch(seeded_db_path):
    p1 = tools.create_meal_plan("2026-04-06", db_path=seeded_db_path)
    p2 = tools.create_meal_plan("2026-04-13", db_path=seeded_db_path)
    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    item = tools.add_recipe_to_plan(p1["id"], recipe["id"], db_path=seeded_db_path)
    with pytest.raises(ValueError):
        tools.remove_meal_plan_item_from_plan(p2["id"], item["id"], db_path=seeded_db_path)


# ---------- concurrency (AC11) ----------


def test_add_to_current_week_plan_concurrent(seeded_db_path):
    """Two threads with Barrier(2) produce exactly 1 meal_plans row and 2 meal_plan_items."""
    import threading

    recipe = tools.search_recipes(db_path=seeded_db_path)[0]
    barrier = threading.Barrier(2, timeout=10)
    results = [None, None]
    errors = [None, None]

    def worker(idx):
        try:
            barrier.wait()
            results[idx] = tools.add_to_current_week_plan(recipe["id"], db_path=seeded_db_path)
        except Exception as e:
            errors[idx] = e

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert errors[0] is None and errors[1] is None
    assert results[0] is not None and results[1] is not None
    assert results[0]["plan_id"] == results[1]["plan_id"]

    from pantry_cooking_vibes.dates import current_sunday

    sunday = current_sunday().isoformat()
    with connect(seeded_db_path) as conn:
        plan_count = conn.execute(
            "SELECT COUNT(*) FROM meal_plans WHERE week_of = ? AND status = 'draft'",
            (sunday,),
        ).fetchone()[0]
        item_count = conn.execute(
            "SELECT COUNT(*) FROM meal_plan_items WHERE plan_id = ?",
            (results[0]["plan_id"],),
        ).fetchone()[0]
    assert plan_count == 1
    assert item_count == 2
