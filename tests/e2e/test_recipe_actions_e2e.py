"""Recipe write-paths: delete + add-to-current-week.

Delete uses a throwaway recipe so seed rows other tests depend on stay intact.
"""

from __future__ import annotations

import pytest

from pantry_cooking_vibes.dates import current_sunday
from pantry_cooking_vibes.db import connect

pytestmark = pytest.mark.e2e


def test_delete_recipe_redirects_and_404s(live_server, page, e2e_db):
    with connect(e2e_db) as conn:
        recipe_id = conn.execute(
            "INSERT INTO recipes (source, source_id, name, cooking_time_min, servings, "
            "                     instructions_md) "
            "VALUES ('manual', 'e2e-throwaway', 'Throwaway Recipe', 10, 1, 'noop') "
            "RETURNING id"
        ).fetchone()["id"]

    page.goto(f"{live_server}/recipes/{recipe_id}")
    page.wait_for_load_state("networkidle")
    assert "Throwaway Recipe" in page.content()

    page.once("dialog", lambda d: d.accept())
    page.locator("form.recipe-delete button").click()
    page.wait_for_load_state("networkidle")

    assert page.url.rstrip("/").endswith("/recipes"), (
        f"delete should redirect to /recipes, got {page.url}"
    )

    response = page.goto(f"{live_server}/recipes/{recipe_id}")
    assert response is not None and response.status == 404

    with connect(e2e_db) as conn:
        gone = conn.execute(
            "SELECT COUNT(*) AS c FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()["c"]
    assert gone == 0


def test_add_to_current_week_creates_plan_and_inserts_item(live_server, page, e2e_db):
    """Form's hidden ``redirect_to`` lands back on /recipes/<id> intentionally;
    verify the side-effect via the DB instead of the URL."""
    target_week = current_sunday().isoformat()
    with connect(e2e_db) as conn:
        recipe_id = conn.execute(
            "SELECT id FROM recipes WHERE name = 'Lemon Chicken Skillet'"
        ).fetchone()["id"]
        items_before = conn.execute(
            "SELECT COUNT(*) AS c FROM meal_plan_items mpi "
            "JOIN meal_plans mp ON mp.id = mpi.plan_id "
            "WHERE mp.week_of = ? AND mp.status = 'draft' AND mpi.recipe_id = ?",
            (target_week, recipe_id),
        ).fetchone()["c"]

    page.goto(f"{live_server}/recipes/{recipe_id}")
    page.wait_for_load_state("networkidle")

    form = page.locator("form.add-to-plan-form")
    form.locator('select[name="week_of"]').select_option(target_week)
    form.locator('button[type="submit"]').click()
    page.wait_for_load_state("networkidle")

    assert f"/recipes/{recipe_id}" in page.url, (
        f"redirect_to should preserve recipe view, got {page.url}"
    )

    with connect(e2e_db) as conn:
        plan = conn.execute(
            "SELECT id FROM meal_plans WHERE week_of = ? AND status = 'draft'",
            (target_week,),
        ).fetchone()
        assert plan is not None, "draft plan for current week should exist"
        plan_id = plan["id"]
        items_after = conn.execute(
            "SELECT COUNT(*) AS c FROM meal_plan_items WHERE plan_id = ? AND recipe_id = ?",
            (plan_id, recipe_id),
        ).fetchone()["c"]
    assert items_after == items_before + 1, "exactly one new item should be added"
