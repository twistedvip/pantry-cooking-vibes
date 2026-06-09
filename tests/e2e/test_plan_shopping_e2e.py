"""Plan detail + shopping list rendering.

Plans are read-only in the web UI (creation lives in MCP/CLI), so this exercises
the plan→shopping pipeline that joins plan items → recipe ingredients →
canonicals → pantry. The seed plan has chicken+olive-oil and tofu; pantry holds
olive oil, so 'covered' must list olive oil and 'needed' must list chicken/tofu.

Also asserts the v1 'qualitative' disclaimer remains on the page so the docs
intent (no quantity math) is still surfaced to users.
"""

from __future__ import annotations

import pytest

from pantry_cooking_vibes.db import connect

pytestmark = pytest.mark.e2e


def _seed_plan_id(e2e_db) -> int:
    with connect(e2e_db) as conn:
        return conn.execute("SELECT id FROM meal_plans WHERE week_of = '2026-05-04'").fetchone()[
            "id"
        ]


def test_plan_detail_lists_seeded_recipes(live_server, page, e2e_db):
    plan_id = _seed_plan_id(e2e_db)
    page.goto(f"{live_server}/plans")
    page.wait_for_load_state("networkidle")

    # Card titles are humanized ("Week of May 4" / "This week"), so target the
    # seed plan by its stable href instead of the date text.
    page.locator(f'li.plan-card a[href="/plans/{plan_id}"]').first.click()
    page.wait_for_load_state("networkidle")

    body = page.content()
    assert "Lemon Chicken Skillet" in body
    assert "Vegan Tofu Bowl" in body
    assert "mon" in body.lower() and "dinner" in body.lower()


def test_shopping_list_separates_needed_from_covered(live_server, page, e2e_db):
    plan_id = _seed_plan_id(e2e_db)
    page.goto(f"{live_server}/plans")
    page.wait_for_load_state("networkidle")
    page.locator(f'li.plan-card a[href="/plans/{plan_id}"]').first.click()
    page.wait_for_load_state("networkidle")

    page.get_by_role("link", name="Shopping list →").click()
    page.wait_for_load_state("networkidle")

    body = page.content()
    assert "qualitative" in body.lower(), "v1 quantity disclaimer must remain visible"

    needed_section = page.locator("section").filter(has_text="Need to buy")
    assert needed_section.count() >= 1
    needed_text = needed_section.first.inner_text().lower()
    assert "chicken breast" in needed_text
    assert "tofu" in needed_text
    assert "olive oil" not in needed_text, (
        "olive oil is in the pantry — must not appear in 'need to buy'"
    )

    covered_section = page.locator("section").filter(has_text="Already in pantry")
    assert covered_section.count() >= 1
    covered_text = covered_section.first.inner_text().lower()
    assert "olive oil" in covered_text


def test_shopping_list_404_for_unknown_plan(live_server, page):
    response = page.goto(f"{live_server}/plans/9999/shopping")
    assert response is not None and response.status == 404
