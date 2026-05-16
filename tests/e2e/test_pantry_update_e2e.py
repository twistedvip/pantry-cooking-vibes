"""Pantry update flow: edit quantity + unit on an existing pantry row.

Restores original values after the write so downstream tests still see the
seeded olive-oil state.
"""

from __future__ import annotations

import pytest

from pantry_cooking_vibes.db import connect

pytestmark = pytest.mark.e2e


def test_pantry_update_changes_quantity_and_unit(live_server, page, e2e_db):
    with connect(e2e_db) as conn:
        original = conn.execute(
            "SELECT p.id, p.quantity, p.unit FROM pantry p "
            "JOIN canonical_ingredients c ON c.id = p.canonical_id "
            "WHERE c.name = 'olive oil'"
        ).fetchone()
    assert original is not None, "seed olive-oil pantry row must exist"
    item_id = original["id"]

    page.goto(f"{live_server}/pantry")
    page.wait_for_load_state("networkidle")

    row = page.locator(".pantry-item").filter(has_text="olive oil").first
    edit = row.locator("form.edit-form")
    edit.locator('input[name="quantity"]').fill("2.5")
    edit.locator('input[name="unit"]').fill("liter")
    edit.locator('button[type="submit"]').click()
    page.wait_for_load_state("networkidle")

    assert "updated" in page.url
    assert "✓ Updated" in page.content()

    with connect(e2e_db) as conn:
        row_db = conn.execute(
            "SELECT quantity, unit FROM pantry WHERE id = ?", (item_id,)
        ).fetchone()
    assert float(row_db["quantity"]) == pytest.approx(2.5)
    assert row_db["unit"] == "liter"

    # Restore so downstream tests still see the original 'bottle' state.
    page.request.post(
        f"{live_server}/pantry/{item_id}/update",
        form={"quantity": str(original["quantity"]), "unit": original["unit"] or ""},
    )


def test_pantry_update_rejects_negative_quantity(live_server, page, e2e_db):
    """HTML5 `min=0` blocks negatives client-side; bypass it via the network
    layer to verify the server-side guard surfaces an ``?error=`` flash."""
    with connect(e2e_db) as conn:
        item_id = conn.execute(
            "SELECT p.id FROM pantry p "
            "JOIN canonical_ingredients c ON c.id = p.canonical_id "
            "WHERE c.name = 'olive oil'"
        ).fetchone()["id"]

    response = page.request.post(
        f"{live_server}/pantry/{item_id}/update",
        form={"quantity": "-1", "unit": "bottle"},
        max_redirects=0,
    )
    assert response.status == 303
    location = response.headers.get("location", "")
    assert "/pantry?error=" in location, f"expected error redirect, got {location!r}"
