"""Tests for the import-inbox web routes."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.importers import import_inbox as inbox, inbox_ingest
from pantry_cooking_vibes.web.app import create_app


@pytest.fixture
def client(db_path) -> TestClient:
    return TestClient(create_app(db_path=db_path))


def _recipe(name, url, image="https://e.com/i.jpg", ings=("1 onion",), steps="Cook."):
    obj = {"@context": "https://schema.org", "@type": "Recipe", "name": name, "url": url}
    if image:
        obj["image"] = image
    if ings:
        obj["recipeIngredient"] = list(ings)
    if steps:
        obj["recipeInstructions"] = steps
    return obj


def _seed_batch(db_path) -> int:
    """A batch with one of each status (ready / review / dup / failed)."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO recipes (source, source_id, name) VALUES ('url', 'https://e.com/dup', 'Twin')"
        )
    text = json.dumps(
        [
            _recipe("Ready One", "https://e.com/a"),
            _recipe("Review One", "https://e.com/b", ings=()),
            _recipe("Dup One", "https://e.com/dup"),
            _recipe("Failed One", "https://e.com/c", image=None),
        ]
    )
    return inbox_ingest.build_batch(file_text=text, file_name="b.jsonld", db_path=db_path)


# ---------- entry + create ----------


def test_imports_home_empty_shows_start_form(client: TestClient):
    r = client.get("/imports", follow_redirects=True)
    assert r.status_code == 200
    assert 'action="/imports"' in r.text
    assert 'name="urls"' in r.text


def test_create_from_file_redirects_to_inbox(client: TestClient):
    text = json.dumps([_recipe("Soup", "https://e.com/soup")])
    r = client.post(
        "/imports",
        data={"urls": ""},
        files={"file": ("recipes.jsonld", text, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/imports/")
    inbox_page = client.get(r.headers["location"])
    assert "Soup" in inbox_page.text


def test_create_with_no_input_is_422(client: TestClient):
    r = client.post("/imports", data={"urls": "   "})
    assert r.status_code == 422
    assert "at least one URL" in r.text


# ---------- inbox rendering ----------


def test_inbox_renders_counts_on_pills(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    r = client.get(f"/imports/{batch_id}")
    assert r.status_code == 200
    # Each status pill carries its count.
    assert "ready" in r.text and "needs review" in r.text
    assert "duplicate" in r.text and "failed" in r.text
    # The four-status set is present as pill classes.
    for cls in ("ib-status ready", "ib-status review", "ib-status dup", "ib-status failed"):
        assert cls in r.text


def test_inbox_404_for_missing_batch(client: TestClient):
    assert client.get("/imports/999").status_code == 404


def test_inbox_filter_narrows_to_status(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    r = client.get(f"/imports/{batch_id}?status=ready")
    assert r.status_code == 200
    assert "Ready One" in r.text
    assert "Failed One" not in r.text


# ---------- save / discard ----------


def test_save_all_promotes_into_library(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    r = client.post(
        f"/imports/{batch_id}/save",
        data={"action": "save-all", "status": "ready"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # The ready item is now a real recipe.
    with connect(db_path) as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM recipes WHERE source = 'url'").fetchall()
        }
    assert "Ready One" in names
    # And it left the unresolved set.
    assert inbox.status_counts(batch_id, db_path=db_path)["ready"] == 0


def test_save_selected_with_replace(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    items = inbox.list_items(batch_id, status="dup", db_path=db_path)["items"]
    dup_id = items[0]["id"]
    r = client.post(
        f"/imports/{batch_id}/save",
        data={"action": "save", "item_ids": [dup_id], "replace_ids": [dup_id]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    got = inbox.get_item(dup_id, db_path=db_path)
    assert got is not None and got["status"] == "saved"


def test_discard_selected(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    items = inbox.list_items(batch_id, status="failed", db_path=db_path)["items"]
    failed_id = items[0]["id"]
    r = client.post(
        f"/imports/{batch_id}/save",
        data={"action": "discard", "item_ids": [failed_id]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    got = inbox.get_item(failed_id, db_path=db_path)
    assert got is not None and got["status"] == "discarded"


def test_failed_item_is_selectable(client: TestClient, db_path):
    """A failed row must render a checkbox so it can be discarded (regression)."""
    batch_id = _seed_batch(db_path)
    failed = inbox.list_items(batch_id, status="failed", db_path=db_path)["items"][0]
    r = client.get(f"/imports/{batch_id}?status=failed")
    assert f'name="item_ids" value="{failed["id"]}"' in r.text
    assert "Discard 1 failed" in r.text


def test_discard_all_failed_via_route(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    r = client.post(
        f"/imports/{batch_id}/save",
        data={"action": "discard-all", "status": "failed"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert inbox.status_counts(batch_id, db_path=db_path)["failed"] == 0


def test_batch_is_deleted_when_fully_resolved(client: TestClient, db_path):
    """An import leaves no record: once every item is resolved, the batch is
    gone and the user lands back on the start form."""
    text = json.dumps([_recipe("Only One", "https://e.com/only")])
    batch_id = inbox_ingest.build_batch(file_text=text, db_path=db_path)
    r = client.post(
        f"/imports/{batch_id}/save",
        data={"action": "save-all", "status": "all"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/imports/new")
    assert inbox.get_batch(batch_id, db_path=db_path) is None


# ---------- fix an item ----------


def test_edit_item_rescues_failed(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    failed = inbox.list_items(batch_id, status="failed", db_path=db_path)["items"][0]
    r = client.post(
        f"/imports/items/{failed['id']}/edit",
        data={
            "name": "Fixed Up",
            "cooking_time_min": "20",
            "servings": "2",
            "image_url": "https://e.com/fixed.jpg",
            "tags": "dinner",
            "ingredients": "1 cup rice",
            "instructions": "Cook the rice.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    got = inbox.get_item(failed["id"], db_path=db_path)
    assert got is not None and got["status"] == "ready"


def test_edit_item_rejects_non_numeric_time(client: TestClient, db_path):
    """A typo'd cooking time is rejected (422), not silently blanked."""
    batch_id = _seed_batch(db_path)
    item = inbox.list_items(batch_id, status="ready", db_path=db_path)["items"][0]
    r = client.post(
        f"/imports/items/{item['id']}/edit",
        data={"name": "Fine", "cooking_time_min": "abc", "image_url": "https://e.com/x.jpg"},
    )
    assert r.status_code == 422


def test_create_batch_over_cap_is_422(client: TestClient, db_path, monkeypatch):
    monkeypatch.setattr(inbox_ingest, "MAX_BATCH_ITEMS", 2)
    text = json.dumps([_recipe(f"R{i}", f"https://e.com/{i}") for i in range(3)])
    r = client.post(
        "/imports",
        data={"urls": ""},
        files={"file": ("big.jsonld", text, "application/json")},
    )
    assert r.status_code == 422
    assert "import inbox handles" in r.text


def test_edit_item_blank_name_is_422(client: TestClient, db_path):
    batch_id = _seed_batch(db_path)
    item = inbox.list_items(batch_id, status="ready", db_path=db_path)["items"][0]
    r = client.post(
        f"/imports/items/{item['id']}/edit",
        data={"name": "  ", "image_url": "https://e.com/x.jpg"},
    )
    assert r.status_code == 422
