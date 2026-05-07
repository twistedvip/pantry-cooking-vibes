"""Recipe detail page renders ingredient pantry-overlap correctly.

Lemon Chicken Skillet has chicken breast (NOT in pantry) and olive oil (IN
pantry). The detail template tags chips with ``have``/``need``/``unmapped``
classes — assert the right chip class for each.
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
    assert page.locator("h1.hero-title").inner_text() == "Lemon Chicken Skillet"


def test_recipe_detail_chip_classes_reflect_pantry(live_server, page):
    page.goto(f"{live_server}/recipes")
    page.get_by_role("link", name="Lemon Chicken Skillet").first.click()
    page.wait_for_load_state("networkidle")

    panel = page.locator("section.ingredient-panel")
    assert panel.count() == 1

    olive_chip = panel.locator(".ingredient-chip").filter(has_text="olive oil")
    assert olive_chip.count() == 1
    olive_class = olive_chip.first.get_attribute("class") or ""
    assert "have" in olive_class, f"olive oil chip should be 'have', got: {olive_class}"

    chicken_chip = panel.locator(".ingredient-chip").filter(has_text="chicken")
    assert chicken_chip.count() == 1
    chicken_class = chicken_chip.first.get_attribute("class") or ""
    assert "need" in chicken_class, f"chicken chip should be 'need', got: {chicken_class}"


def test_recipe_detail_404_for_unknown_id(live_server, page):
    response = page.goto(f"{live_server}/recipes/999999")
    assert response is not None and response.status == 404
