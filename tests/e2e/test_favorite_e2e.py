"""Favorite-toggle flow.

CLAUDE.md highlights ``redirect_to`` preservation as a fragile path: the form
includes ``request.url.path`` + ``request.url.query`` as a hidden field so the
303 round-trip lands the user back on the *same filtered list view* they came
from. This test asserts both the on/off DB effect and the redirect preservation.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_favorite_toggle_from_list_preserves_query(live_server, page):
    list_url = f"{live_server}/recipes?q=chicken&max_time=30"
    page.goto(list_url)
    page.wait_for_load_state("networkidle")

    card = page.locator("li.recipe-card").filter(has_text="Lemon Chicken Skillet").first
    assert card.count() == 1

    fav_btn = card.locator("button.fav-btn")
    initially_on = "on" in (fav_btn.get_attribute("class") or "")

    fav_btn.click()
    page.wait_for_load_state("networkidle")

    assert "q=chicken" in page.url, "redirect_to must preserve query string"
    assert "max_time=30" in page.url

    card_after = page.locator("li.recipe-card").filter(has_text="Lemon Chicken Skillet").first
    fav_btn_after = card_after.locator("button.fav-btn")
    now_on = "on" in (fav_btn_after.get_attribute("class") or "")
    assert now_on != initially_on, "favorite state should have flipped"

    fav_btn_after.click()
    page.wait_for_load_state("networkidle")
    final_btn = (
        page.locator("li.recipe-card")
        .filter(has_text="Lemon Chicken Skillet")
        .first.locator("button.fav-btn")
    )
    final_on = "on" in (final_btn.get_attribute("class") or "")
    assert final_on == initially_on, "second toggle should restore original state"


def test_favorite_only_filter_shows_only_favorites(live_server, page):
    page.goto(f"{live_server}/recipes")
    page.wait_for_load_state("networkidle")

    target = page.locator("li.recipe-card").filter(has_text="Vegan Tofu Bowl").first
    fav_btn = target.locator("button.fav-btn")
    if "on" not in (fav_btn.get_attribute("class") or ""):
        fav_btn.click()
        page.wait_for_load_state("networkidle")

    page.goto(f"{live_server}/recipes?fav=1")
    page.wait_for_load_state("networkidle")
    body = page.content()
    assert "Vegan Tofu Bowl" in body
    assert "Lemon Chicken Skillet" not in body, "fav=1 must hide non-favorited recipes"

    final = page.locator("li.recipe-card").filter(has_text="Vegan Tofu Bowl").first
    final.locator("button.fav-btn").click()
    page.wait_for_load_state("networkidle")
