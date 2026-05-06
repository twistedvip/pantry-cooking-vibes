"""Mapping-queue review flow.

Two seeded 'proposed' rows: 'lemon zest' (approve flow) and 'mystery spice'
(reject flow). Approve from the detail page, reject from the list page.
Confirms 303 round-trip lands on the list with the right flash and that the
status filter actually filters.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_mapping_list_shows_proposed_rows(live_server, page):
    page.goto(f"{live_server}/mappings")
    page.wait_for_load_state("networkidle")

    body = page.content()
    assert "lemon zest" in body
    assert "mystery spice" in body
    assert "82%" in body  # confidence column for lemon zest


def test_mapping_approve_from_detail_moves_to_approved(live_server, page):
    page.goto(f"{live_server}/mappings")
    page.wait_for_load_state("networkidle")

    page.locator('a').filter(has_text="lemon zest").first.click()
    page.wait_for_load_state("networkidle")
    assert "/mappings/" in page.url

    page.get_by_role("button", name="Approve as proposed").click()
    page.wait_for_load_state("networkidle")

    assert "approved=" in page.url
    assert "✓ Approved" in page.content()

    page.goto(f"{live_server}/mappings?status=approved")
    page.wait_for_load_state("networkidle")
    assert "lemon zest" in page.content()


def test_mapping_reject_from_list_dismisses_row(live_server, page):
    page.goto(f"{live_server}/mappings")
    page.wait_for_load_state("networkidle")

    row = page.locator('tr').filter(has_text="mystery spice").first
    page.once("dialog", lambda d: d.accept())
    row.locator('form[action$="/reject"] button').click()
    page.wait_for_load_state("networkidle")

    assert "rejected=" in page.url
    assert "✓ Rejected" in page.content()

    body = page.content()
    assert "mystery spice" not in body, "rejected row must drop off the proposed view"

    page.goto(f"{live_server}/mappings?status=rejected")
    page.wait_for_load_state("networkidle")
    assert "mystery spice" in page.content()
