"""Browser-driven smoke tests for the recipe search flow.

These exercise the bugs that unit tests miss because they only surface
when a real HTML form submits its real query string (e.g. empty number
fields become ``max_time=``, which used to 422).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_recipe_search_chicken_does_not_422(live_server, page):
    """Typing a text query and hitting Filter must not produce an
    'expects integer' error, even with max_time and tags blank."""
    page.goto(f"{live_server}/recipes")
    page.fill('input[name="q"]', "chicken")
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")

    body = page.content().lower()
    assert "integer" not in body, "regression: blank numeric field rejected"
    assert "unprocessable" not in body
    assert page.locator('input[name="q"]').input_value() == "chicken"
    assert "lemon chicken skillet" in body


def test_recipe_search_no_match_shows_empty_state(live_server, page):
    page.goto(f"{live_server}/recipes")
    page.fill('input[name="q"]', "zzznothingmatcheszzzz")
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")

    assert "no recipes match" in page.content().lower()


def test_recipe_max_time_filter(live_server, page):
    """Max-time filter submitted as a real number still works."""
    page.goto(f"{live_server}/recipes")
    page.fill('input[name="max_time"]', "22")
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")

    body = page.content().lower()
    assert "vegan tofu bowl" in body  # 20 min <= 22
    assert "lemon chicken skillet" not in body  # 25 min > 22


def test_home_links_to_recipes(live_server, page):
    page.goto(f"{live_server}/")
    page.get_by_role("link", name="Recipes").first.click()
    page.wait_for_load_state("networkidle")
    assert page.url.rstrip("/").endswith("/recipes")


def test_pantry_page_loads(live_server, page):
    page.goto(f"{live_server}/pantry")
    assert (
        page.locator("h1").first.inner_text().lower().startswith("pantry")
        or "pantry" in page.content().lower()
    )


def test_plans_page_loads(live_server, page):
    page.goto(f"{live_server}/plans")
    assert page.locator("body").is_visible()
