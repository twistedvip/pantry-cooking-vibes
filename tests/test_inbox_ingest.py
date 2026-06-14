"""Tests for parsing URLs / JSON-LD files into import-inbox staging items."""

from __future__ import annotations

import json

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.importers import import_inbox as inbox, inbox_ingest


def _recipe_jsonld(
    name="Onion Soup",
    url="https://e.com/soup",
    image="https://e.com/s.jpg",
    ingredients=("1 onion",),
    instructions="Simmer.",
):
    obj = {"@context": "https://schema.org", "@type": "Recipe", "name": name}
    if url:
        obj["url"] = url
    if image:
        obj["image"] = image
    if ingredients:
        obj["recipeIngredient"] = list(ingredients)
    if instructions:
        obj["recipeInstructions"] = instructions
    return obj


def _html(obj) -> str:
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(obj)}</script></head></html>'
    )


# ---------- parse_url ----------


def test_parse_url_ready(db_path):
    item = inbox_ingest.parse_url("https://e.com/soup", existing={}, html=_html(_recipe_jsonld()))
    assert item["status"] == "ready"
    assert item["name"] == "Onion Soup"
    assert item["source"] == "url"
    assert item["ingredients"] == ["1 onion"]


def test_parse_url_no_recipe_is_failed(db_path):
    item = inbox_ingest.parse_url(
        "https://e.com/x", existing={}, html="<html><body>hi</body></html>"
    )
    assert item["status"] == "failed"
    assert item["failure_reason"] == "no recipe data on the page"


def test_parse_url_no_image_is_failed(db_path):
    item = inbox_ingest.parse_url(
        "https://e.com/x", existing={}, html=_html(_recipe_jsonld(image=None))
    )
    assert item["status"] == "failed"
    assert item["failure_reason"] == "no image"


def test_parse_url_missing_ingredients_is_review(db_path):
    item = inbox_ingest.parse_url(
        "https://e.com/x", existing={}, html=_html(_recipe_jsonld(ingredients=()))
    )
    assert item["status"] == "review"
    assert item["review_reason"] == "no ingredients parsed"


def test_parse_url_dup_against_library(db_path):
    existing = {("url", "https://e.com/soup"): 42}
    item = inbox_ingest.parse_url(
        "https://e.com/soup", existing=existing, html=_html(_recipe_jsonld())
    )
    assert item["status"] == "dup"
    assert item["dup_of_recipe_id"] == 42


def test_parse_url_unsafe_address_is_failed(db_path, monkeypatch):
    def boom(url):
        raise inbox_ingest.url_import.UnsafeURLError("nope")

    monkeypatch.setattr(inbox_ingest.url_import, "fetch_html", boom)
    item = inbox_ingest.parse_url("http://169.254.169.254/", existing={})
    assert item["status"] == "failed"
    assert "private" in item["failure_reason"]


# ---------- parse_jsonld_text ----------


def test_parse_jsonld_array(db_path):
    text = json.dumps(
        [
            _recipe_jsonld(name="A", url="https://e.com/a"),
            _recipe_jsonld(name="B", url="https://e.com/b"),
        ]
    )
    items = inbox_ingest.parse_jsonld_text(text, existing={})
    assert len(items) == 2
    assert {i["name"] for i in items} == {"A", "B"}
    assert all(i["status"] == "ready" for i in items)


def test_parse_jsonld_graph_wrapper(db_path):
    text = json.dumps({"@graph": [_recipe_jsonld(name="Graphed", url="https://e.com/g")]})
    items = inbox_ingest.parse_jsonld_text(text, existing={})
    assert len(items) == 1
    assert items[0]["name"] == "Graphed"


def test_parse_jsonld_no_url_gets_synthesized_key(db_path):
    text = json.dumps(_recipe_jsonld(name="No URL Recipe", url=None))
    items = inbox_ingest.parse_jsonld_text(text, existing={})
    assert items[0]["source"] == "jsonld"
    assert items[0]["source_id"] == "jsonld:no-url-recipe"


def test_parse_jsonld_invalid_json_is_failed(db_path):
    items = inbox_ingest.parse_jsonld_text("{not json", existing={})
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert items[0]["failure_reason"] == "file is not valid JSON"


def test_parse_jsonld_no_recipes_is_failed(db_path):
    text = json.dumps({"@type": "WebPage", "name": "Not a recipe"})
    items = inbox_ingest.parse_jsonld_text(text, existing={})
    assert items[0]["failure_reason"] == "no recipes found in the file"


# ---------- build_batch ----------


def test_build_batch_from_file_text(db_path):
    text = json.dumps(
        [
            _recipe_jsonld(name="A", url="https://e.com/a"),
            _recipe_jsonld(name="B", url="https://e.com/b", image=None),  # fails: no image
        ]
    )
    batch_id = inbox_ingest.build_batch(file_text=text, file_name="recipes.jsonld", db_path=db_path)
    batch = inbox.get_batch(batch_id, db_path=db_path)
    assert batch is not None
    assert batch["status"] == "ready"
    assert batch["label"] == "recipes.jsonld"
    assert batch["counts"]["ready"] == 1
    assert batch["counts"]["failed"] == 1
    assert batch["total"] == 2


def test_build_batch_from_urls(db_path, monkeypatch):
    pages = {
        "https://e.com/a": _html(_recipe_jsonld(name="Alpha", url="https://e.com/a")),
        "https://e.com/b": _html(_recipe_jsonld(name="Beta", url="https://e.com/b")),
    }
    monkeypatch.setattr(inbox_ingest.url_import, "fetch_html", lambda url: pages[url])
    batch_id = inbox_ingest.build_batch(
        urls=["https://e.com/a", "https://e.com/b"], db_path=db_path
    )
    batch = inbox.get_batch(batch_id, db_path=db_path)
    assert batch is not None
    assert batch["label"] == "2 URLs"
    assert batch["counts"]["ready"] == 2


def test_build_batch_dedups_against_library(db_path, monkeypatch):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO recipes (source, source_id, name) VALUES ('url', 'https://e.com/a', 'Existing')"
        )
    monkeypatch.setattr(
        inbox_ingest.url_import,
        "fetch_html",
        lambda url: _html(_recipe_jsonld(name="Alpha", url="https://e.com/a")),
    )
    batch_id = inbox_ingest.build_batch(urls=["https://e.com/a"], db_path=db_path)
    batch = inbox.get_batch(batch_id, db_path=db_path)
    assert batch is not None
    assert batch["counts"]["dup"] == 1
