"""Verify the UI's 'works with JS disabled' invariant from CLAUDE.md.

The repo intentionally avoids a JS build step — every interactive control is a
plain ``<form>``. Disable JS in the browser context and exercise the core
search + favorite-toggle flows; both must still work because the server does
all the work via 303 redirects.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_recipe_search_works_with_js_disabled(live_server, browser):
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(f"{live_server}/recipes")
        page.fill('input[name="q"]', "chicken")
        page.click('button[type="submit"]')
        page.wait_for_load_state("load")

        body = page.content().lower()
        assert "lemon chicken skillet" in body
        assert "integer" not in body
    finally:
        context.close()


def _chicken_fav_btn(page):
    return (
        page.locator("li.recipe-card")
        .filter(has_text="Lemon Chicken Skillet")
        .first.locator("button.fav-btn")
    )


_ON_RE = re.compile(r"\bon\b")


def _toggle_and_wait(page) -> None:
    """Click chicken's fav button and wait for the post-redirect page to settle.

    With JS disabled the form does POST → 303 → GET; redirect_to lands on the
    same URL, so URL-watchers (expect_navigation, wait_for_url) never fire.
    Wait on the POST response itself, then settle the document.
    """
    with page.expect_response(re.compile(r"/recipes/\d+/favorite")):
        _chicken_fav_btn(page).click()
    page.wait_for_load_state("networkidle")


def test_favorite_toggle_works_with_js_disabled(live_server, browser):
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(f"{live_server}/recipes", wait_until="domcontentloaded")

        before_class = _chicken_fav_btn(page).get_attribute("class") or ""
        before_on = bool(_ON_RE.search(before_class))

        _toggle_and_wait(page)
        if before_on:
            expect(_chicken_fav_btn(page)).not_to_have_class(_ON_RE)
        else:
            expect(_chicken_fav_btn(page)).to_have_class(_ON_RE)

        _toggle_and_wait(page)
        if before_on:
            expect(_chicken_fav_btn(page)).to_have_class(_ON_RE)
        else:
            expect(_chicken_fav_btn(page)).not_to_have_class(_ON_RE)
    finally:
        context.close()
