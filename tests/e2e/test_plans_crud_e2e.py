"""Plan write-paths: list, create, favorite, clone, item-delete, print.

Inserts throwaway plan/item rows where deletion would corrupt the session seed.
"""

from __future__ import annotations

import re

import pytest

from pantry_cooking_vibes.db import connect

pytestmark = pytest.mark.e2e


def test_plans_list_shows_seeded_plan(live_server, page):
    page.goto(f"{live_server}/plans")
    page.wait_for_load_state("networkidle")

    card = page.locator("li.plan-card").filter(has_text="2026-05-04")
    assert card.count() == 1
    text = card.first.inner_text().lower()
    assert "2 meals" in text
    assert "% pantry" in text


def test_create_plan_redirects_to_plan_detail(live_server, page):
    page.goto(f"{live_server}/plans")
    page.wait_for_load_state("networkidle")

    page.get_by_role("button", name="+ New plan for this week").click()
    page.wait_for_load_state("networkidle")

    assert re.search(r"/plans/\d+$", page.url), f"expected /plans/<id>, got {page.url}"
    h1 = page.locator("h1").first.inner_text()
    assert h1.startswith("Week of "), f"detail h1 missing: {h1!r}"


def _is_plan_favorite(conn, plan_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM meal_plan_favorites WHERE plan_id = ?", (plan_id,)).fetchone()
    return row is not None


def test_plan_favorite_toggles_on_off(live_server, page, e2e_db):
    with connect(e2e_db) as conn:
        plan_id = conn.execute("SELECT id FROM meal_plans WHERE week_of = '2026-05-04'").fetchone()[
            "id"
        ]
        initial = _is_plan_favorite(conn, plan_id)

    page.goto(f"{live_server}/plans/{plan_id}")
    page.wait_for_load_state("networkidle")

    page.locator("form.fav-toggle button.fav-btn").click()
    page.wait_for_load_state("networkidle")

    with connect(e2e_db) as conn:
        after_first = _is_plan_favorite(conn, plan_id)
    assert after_first != initial, "first toggle should flip favorite state"
    assert f"/plans/{plan_id}" in page.url, "redirect_to should land back on plan detail"

    page.locator("form.fav-toggle button.fav-btn").click()
    page.wait_for_load_state("networkidle")
    with connect(e2e_db) as conn:
        final = _is_plan_favorite(conn, plan_id)
    assert final == initial, "second toggle should restore original state"


def test_plan_clone_creates_new_draft(live_server, page, e2e_db):
    with connect(e2e_db) as conn:
        source_id = conn.execute(
            "SELECT id FROM meal_plans WHERE week_of = '2026-05-04'"
        ).fetchone()["id"]
        before_count = conn.execute("SELECT COUNT(*) AS c FROM meal_plans").fetchone()["c"]

    page.goto(f"{live_server}/plans/{source_id}")
    page.wait_for_load_state("networkidle")

    page.get_by_role("button", name="Clone").click()
    page.wait_for_load_state("networkidle")

    m = re.search(r"/plans/(\d+)$", page.url)
    assert m, f"expected redirect to /plans/<id>, got {page.url}"
    new_id = int(m.group(1))
    assert new_id != source_id, "clone must produce a new plan id"

    with connect(e2e_db) as conn:
        after_count = conn.execute("SELECT COUNT(*) AS c FROM meal_plans").fetchone()["c"]
        notes = conn.execute("SELECT notes FROM meal_plans WHERE id = ?", (new_id,)).fetchone()[
            "notes"
        ]
    assert after_count == before_count + 1, "clone should add exactly one plan row"
    assert f"Cloned from #{source_id}" in (notes or "")


def test_plan_item_delete_removes_row(live_server, page, e2e_db):
    """Insert throwaway plan+item so deletion doesn't corrupt the seed plan."""
    with connect(e2e_db) as conn:
        recipe_id = conn.execute(
            "SELECT id FROM recipes WHERE name = 'Vegan Tofu Bowl'"
        ).fetchone()["id"]
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of, status, notes) "
            "VALUES ('2026-04-19', 'draft', 'item-delete test') RETURNING id"
        ).fetchone()["id"]
        item_id = conn.execute(
            "INSERT INTO meal_plan_items "
            "(plan_id, recipe_id, day, meal_slot, servings_planned) "
            "VALUES (?, ?, 'wed', 'dinner', 2) RETURNING id",
            (plan_id, recipe_id),
        ).fetchone()["id"]

    page.goto(f"{live_server}/plans/{plan_id}")
    page.wait_for_load_state("networkidle")
    assert "Vegan Tofu Bowl" in page.content()

    page.locator(f'form[action="/plans/{plan_id}/items/{item_id}/delete"] button').click()
    page.wait_for_load_state("networkidle")

    assert f"/plans/{plan_id}" in page.url, "redirect_to should preserve plan view"
    body = page.content().lower()
    assert "no recipes yet" in body, "empty-state should appear after last item removed"

    with connect(e2e_db) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM meal_plan_items WHERE id = ?", (item_id,)
        ).fetchone()["c"]
    assert remaining == 0


def test_plan_print_renders_shopping_and_recipe_cards(live_server, page, e2e_db):
    with connect(e2e_db) as conn:
        plan_id = conn.execute("SELECT id FROM meal_plans WHERE week_of = '2026-05-04'").fetchone()[
            "id"
        ]

    response = page.goto(f"{live_server}/plans/{plan_id}/print")
    assert response is not None and response.status == 200

    shopping = page.locator('[data-role="ingredients-page"]')
    assert shopping.count() == 1
    shopping_text = shopping.inner_text().lower()
    assert "chicken breast" in shopping_text
    assert "olive oil" in shopping_text
    assert "tofu" in shopping_text

    recipe_cards = page.locator('[data-role="recipe-page"]')
    assert recipe_cards.count() == 2, "print view should render one card per plan item"
    all_text = recipe_cards.all_inner_texts()
    joined = " ".join(all_text)
    assert "Lemon Chicken Skillet" in joined
    assert "Vegan Tofu Bowl" in joined


def test_plan_print_404_for_unknown_plan(live_server, page):
    response = page.goto(f"{live_server}/plans/999999/print")
    assert response is not None and response.status == 404
