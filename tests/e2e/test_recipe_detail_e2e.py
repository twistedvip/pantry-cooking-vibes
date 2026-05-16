"""Recipe detail page renders ingredient pantry-overlap correctly.

Lemon Chicken Skillet has chicken breast (NOT in pantry) and olive oil (IN
pantry). The detail template tags ingredient rows with
``have``/``need``/``unmapped`` classes — assert the right class for each.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_recipe_detail_navigated_from_list(live_server, page):
    page.goto(f"{live_server}/recipes")
    page.wait_for_load_state("networkidle")

    page.get_by_role("link", name="Lemon Chicken Skillet").first.click()
    page.wait_for_load_state("networkidle")

    assert "/recipes/" in page.url
    assert page.locator("h1.detail-title").inner_text() == "Lemon Chicken Skillet"


def test_recipe_detail_chip_classes_reflect_pantry(live_server, page):
    page.goto(f"{live_server}/recipes")
    page.get_by_role("link", name="Lemon Chicken Skillet").first.click()
    page.wait_for_load_state("networkidle")

    panel = page.locator("aside.ingredient-card")
    assert panel.count() == 1

    olive_row = panel.locator("li.ingredient-row").filter(has_text="olive oil")
    assert olive_row.count() == 1
    olive_class = olive_row.first.get_attribute("class") or ""
    assert "have" in olive_class, f"olive oil row should be 'have', got: {olive_class}"

    chicken_row = panel.locator("li.ingredient-row").filter(has_text="chicken")
    assert chicken_row.count() == 1
    chicken_class = chicken_row.first.get_attribute("class") or ""
    assert "need" in chicken_class, f"chicken row should be 'need', got: {chicken_class}"


def test_recipe_detail_404_for_unknown_id(live_server, page):
    response = page.goto(f"{live_server}/recipes/999999")
    assert response is not None and response.status == 404
