"""Pantry add/remove flow — the only read/write surface in the web UI besides mappings.

Search for a canonical, add it via the suggestions form, confirm it lands in the
list, then remove it. Verifies the form-encoded redirect protocol
(``303`` → ``?added=`` / ``?removed=`` flash) actually round-trips.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_pantry_add_then_remove_round_trips(live_server, page):
    page.goto(f"{live_server}/pantry")

    page.fill('input[name="search"]', "salt")
    page.click('form.filters button[type="submit"]')
    page.wait_for_load_state("networkidle")

    suggestion = page.locator('ul.suggestions form.add-form').filter(
        has_text="salt"
    ).first
    assert suggestion.count() == 1, "expected exactly one 'salt' suggestion form"

    suggestion.locator('input[name="quantity"]').fill("3")
    suggestion.locator('input[name="unit"]').fill("tsp")
    suggestion.locator('button[type="submit"]').click()
    page.wait_for_load_state("networkidle")

    assert "added" in page.url
    assert "✓ Added" in page.content()

    pantry_row = page.locator('.pantry-item').filter(has_text="salt")
    assert pantry_row.count() >= 1, "salt should now appear in 'In your pantry'"

    page.once("dialog", lambda d: d.accept())
    pantry_row.first.locator('form[action$="/delete"] button').click()
    page.wait_for_load_state("networkidle")

    assert "removed" in page.url
    assert page.locator('.pantry-item').filter(has_text="salt").count() == 0


def test_pantry_invalid_search_shows_empty_state(live_server, page):
    page.goto(f"{live_server}/pantry?search=zzznopezzz")
    body = page.content().lower()
    assert "no canonical ingredient matches" in body
