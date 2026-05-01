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


# ---------- meal plan ----------


def test_create_meal_plan_invalid_week(seeded_db_path):
    with pytest.raises(ValueError):
        tools.create_meal_plan("not-a-date", db_path=seeded_db_path)


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
    assert len(detail["items"]) == 1
    assert "recipe_name" in detail["items"][0]

    assert tools.remove_meal_plan_item(item["id"], db_path=seeded_db_path)["removed"] is True
    assert tools.get_meal_plan(plan["id"], db_path=seeded_db_path)["items"] == []


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
