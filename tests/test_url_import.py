"""Tests for Phase 5 url-import: schema.org Recipe JSON-LD parser + DB writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.importers.url_import import (
    RecipeMissingImageError,
    RecipeNotFoundError,
    _collect_tags,
    _image_url,
    _instructions,
    _rating,
    _text,
    extract_recipe_jsonld,
    import_url,
    parse_iso_duration,
    parse_recipe,
    parse_yield,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "url_import"
SERIOUS_EATS_URL = "https://www.seriouseats.com/pasta-al-tonno-5135115"


# ---------- ISO 8601 duration ----------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("PT30M", 30),
        ("PT1H", 60),
        ("PT1H30M", 90),
        ("PT2H15M", 135),
        ("PT0M", 0),
        ("P1D", 24 * 60),
        ("PT45S", 1),  # round half-up
        ("PT15S", 0),  # round down
        ("", None),
        (None, None),
        ("30 minutes", None),
        ("garbage", None),
        (12345, None),
    ],
)
def test_parse_iso_duration(value, expected):
    assert parse_iso_duration(value) == expected


# ---------- recipeYield ----------


@pytest.mark.parametrize(
    "value, expected",
    [
        (4, 4),
        ("4", 4),
        ("4 servings", 4),
        (["4", "4 servings"], 4),
        (["serves four"], None),
        ("0", None),
        (None, None),
        ("", None),
        (3.0, 3),
    ],
)
def test_parse_yield(value, expected):
    assert parse_yield(value) == expected


# ---------- image / text / rating helpers ----------


def test_image_url_handles_str_dict_list():
    assert _image_url("https://example.com/a.jpg") == "https://example.com/a.jpg"
    assert _image_url({"url": "https://example.com/b.jpg"}) == "https://example.com/b.jpg"
    assert (
        _image_url(["https://example.com/c.jpg", "https://example.com/d.jpg"])
        == "https://example.com/c.jpg"
    )
    assert _image_url(None) is None
    assert _image_url({"foo": "bar"}) is None


def test_text_joins_lists_and_strips():
    assert _text("  hello  ") == "hello"
    assert _text(["a", "b", "c"]) == "a, b, c"
    assert _text(None) is None
    assert _text("") is None
    assert _text({"name": "Title"}) == "Title"


def test_rating_pulls_from_aggregate():
    val, cnt = _rating({"aggregateRating": {"ratingValue": "4.6", "ratingCount": "7"}})
    assert val == 4.6
    assert cnt == 7
    val, cnt = _rating({"aggregateRating": {"ratingValue": "5.0", "reviewCount": "2"}})
    assert val == 5.0 and cnt == 2
    assert _rating({}) == (None, None)
    assert _rating({"aggregateRating": "bogus"}) == (None, None)


# ---------- instruction rendering ----------


def test_instructions_handles_string():
    assert _instructions("<p>Heat oil.</p>") == "Heat oil."


def test_instructions_handles_HowToStep_list():
    out = _instructions(
        [
            {"@type": "HowToStep", "text": "Step one."},
            {"@type": "HowToStep", "text": "Step two."},
        ]
    )
    assert out is not None
    assert "Step one." in out and "Step two." in out


def test_instructions_handles_HowToSection():
    out = _instructions(
        [
            {
                "@type": "HowToSection",
                "name": "Sauce",
                "itemListElement": [
                    {"@type": "HowToStep", "text": "Simmer tomatoes."},
                ],
            },
            {
                "@type": "HowToSection",
                "name": "Pasta",
                "itemListElement": [
                    {"@type": "HowToStep", "text": "Boil water."},
                ],
            },
        ]
    )
    assert out is not None
    assert "## Sauce" in out and "Simmer tomatoes." in out
    assert "## Pasta" in out and "Boil water." in out


def test_instructions_returns_none_for_empty():
    assert _instructions(None) is None
    assert _instructions([]) is None


# ---------- tag collection ----------


def test_collect_tags_dedups_case_insensitive():
    tags = _collect_tags(
        {
            "keywords": "Quick, Italian, italian",
            "recipeCategory": ["Mains", "Quick"],
            "recipeCuisine": ["Italian"],
        }
    )
    assert tags == ["quick", "italian", "mains"]


# ---------- JSON-LD extraction ----------


def test_extract_handles_list_block():
    html = '<script type="application/ld+json">[{"@type":"Recipe","name":"X"}]</script>'
    assert extract_recipe_jsonld(html)["name"] == "X"


def test_extract_handles_graph_wrapper():
    html = (
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"WebPage"},{"@type":"Recipe","name":"Y"}]}'
        "</script>"
    )
    assert extract_recipe_jsonld(html)["name"] == "Y"


def test_extract_handles_array_type():
    html = '<script type="application/ld+json">{"@type":["Recipe","Article"],"name":"Z"}</script>'
    assert extract_recipe_jsonld(html)["name"] == "Z"


def test_extract_returns_none_when_no_recipe():
    html = '<script type="application/ld+json">{"@type":"WebPage","name":"page"}</script>'
    assert extract_recipe_jsonld(html) is None


def test_extract_skips_invalid_json_blocks():
    html = (
        '<script type="application/ld+json">{not json}</script>'
        '<script type="application/ld+json">{"@type":"Recipe","name":"OK"}</script>'
    )
    assert extract_recipe_jsonld(html)["name"] == "OK"


def test_extract_no_jsonld_at_all():
    assert extract_recipe_jsonld("<html><body>nothing here</body></html>") is None


# ---------- parse_recipe end-to-end ----------


def test_parse_recipe_synthetic():
    entity = {
        "@type": "Recipe",
        "name": "Test Dish",
        "totalTime": "PT45M",
        "recipeYield": "6 servings",
        "image": {"@type": "ImageObject", "url": "https://example.com/img.jpg"},
        "aggregateRating": {"ratingValue": "4.5", "ratingCount": "10"},
        "nutrition": {"@type": "NutritionInformation", "calories": "300"},
        "recipeIngredient": ["1 cup flour", "2 eggs", "  "],  # blank should drop
        "recipeInstructions": [{"@type": "HowToStep", "text": "Mix."}],
        "keywords": "easy, quick",
        "recipeCategory": "Dessert",
    }
    rec = parse_recipe(entity, "https://example.com/test")
    assert rec["source_id"] == "https://example.com/test"
    assert rec["name"] == "Test Dish"
    assert rec["cooking_time_min"] == 45
    assert rec["servings"] == 6
    assert rec["image_url"] == "https://example.com/img.jpg"
    assert rec["rating"] == 4.5
    assert rec["rating_count"] == 10
    nutrition = json.loads(rec["nutrition_json"])
    # Compact macro dict: strings coerced to floats, schema.org @type stripped,
    # and non-macro fields dropped entirely.
    assert nutrition["calories"] == 300.0 and "@type" not in nutrition
    assert set(nutrition.keys()) <= {
        "calories",
        "protein_g",
        "fat_g",
        "carbs_g",
        "fiber_g",
        "sodium_mg",
    }
    assert rec["ingredients"] == ["1 cup flour", "2 eggs"]
    assert rec["tags"] == ["easy", "quick", "dessert"]
    assert "Mix." in rec["instructions_md"]


def test_parse_recipe_falls_back_to_cookTime_when_total_missing():
    entity = {"@type": "Recipe", "name": "X", "cookTime": "PT20M"}
    assert parse_recipe(entity, "u")["cooking_time_min"] == 20


# ---------- Serious Eats fixture ----------


def test_parse_serious_eats_fixture():
    html = (FIXTURE_DIR / "seriouseats_pasta_al_tonno.html").read_text(encoding="utf-8")
    entity = extract_recipe_jsonld(html)
    assert entity is not None

    rec = parse_recipe(entity, SERIOUS_EATS_URL)
    assert "Pasta al Tonno" in rec["name"]
    assert rec["cooking_time_min"] == 30
    assert rec["servings"] == 4
    assert rec["rating"] == 4.6
    assert rec["rating_count"] == 7
    assert rec["image_url"] and rec["image_url"].startswith("https://")
    assert len(rec["ingredients"]) == 9
    assert any("olive oil" in s.lower() for s in rec["ingredients"])
    assert rec["instructions_md"] and len(rec["instructions_md"]) > 200
    assert "italian" in rec["tags"]
    nutrition = json.loads(rec["nutrition_json"])
    assert "calories" in nutrition


# ---------- import_url DB integration ----------


def test_import_url_basic(db_path):
    html = (FIXTURE_DIR / "seriouseats_pasta_al_tonno.html").read_text(encoding="utf-8")
    stats = import_url(SERIOUS_EATS_URL, db_path=db_path, html=html, quiet=True)

    assert stats["ingredients"] == 9
    assert stats["tags"] >= 1

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT source, source_id, name, servings, rating FROM recipes WHERE id=?",
            (stats["recipe_id"],),
        ).fetchone()
    assert row["source"] == "url"
    assert row["source_id"] == SERIOUS_EATS_URL
    assert row["servings"] == 4
    assert row["rating"] == 4.6


def test_import_url_idempotent(db_path):
    html = (FIXTURE_DIR / "seriouseats_pasta_al_tonno.html").read_text(encoding="utf-8")
    import_url(SERIOUS_EATS_URL, db_path=db_path, html=html, quiet=True)
    import_url(SERIOUS_EATS_URL, db_path=db_path, html=html, quiet=True)

    with connect(db_path) as conn:
        recipes = conn.execute("SELECT COUNT(*) FROM recipes WHERE source='url'").fetchone()[0]
        ings = conn.execute("SELECT COUNT(*) FROM recipe_ingredients").fetchone()[0]
        tags = conn.execute("SELECT COUNT(*) FROM recipe_tags").fetchone()[0]
    assert recipes == 1
    assert ings == 9
    assert tags >= 1


def test_import_url_uses_canonical_map(db_path):
    """Pre-seed the queue with a URL-import mapping; verify canonical_id flows through."""
    html = (FIXTURE_DIR / "seriouseats_pasta_al_tonno.html").read_text(encoding="utf-8")
    raw_text = "3 tablespoons (45ml) extra-virgin olive oil"

    with connect(db_path) as conn:
        canonical_id = conn.execute(
            "INSERT INTO canonical_ingredients (name, category) VALUES "
            "('test-olive-oil-canonical', 'oil') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO ingredient_mapping_queue
                (source, source_key, original_text, proposed_canonical_id, confidence, status)
            VALUES ('url_import', ?, ?, ?, 0.95, 'approved')
            """,
            (raw_text, raw_text, canonical_id),
        )

    import_url(SERIOUS_EATS_URL, db_path=db_path, html=html, quiet=True)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT canonical_id FROM recipe_ingredients WHERE original_text=?",
            (raw_text,),
        ).fetchone()
    assert row["canonical_id"] == canonical_id


def test_import_url_auto_enqueues_each_ingredient(db_path):
    """Every ingredient string should land in ingredient_mapping_queue under
    source='url_import' so curators don't have to discover them manually."""
    html = (FIXTURE_DIR / "seriouseats_pasta_al_tonno.html").read_text(encoding="utf-8")
    import_url(SERIOUS_EATS_URL, db_path=db_path, html=html, quiet=True)

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source_key, original_text FROM ingredient_mapping_queue "
            "WHERE source = 'url_import'"
        ).fetchall()
    queued_keys = {r["source_key"] for r in rows}
    # Recipe has 9 ingredients; each should be queued exactly once.
    assert len(queued_keys) == 9
    assert any("olive oil" in k.lower() for k in queued_keys)


def test_import_url_auto_approved_ingredient_resolves_to_canonical(db_path):
    """Seed a canonical that exactly matches an ingredient string; importing
    should auto-enqueue it as 'approved' and link recipe_ingredients.canonical_id."""
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Recipe","name":"Onion Test",'
        '"image":"https://example.com/onion.jpg",'
        '"recipeIngredient":["onion"]}'
        "</script>"
    )
    with connect(db_path) as conn:
        row = conn.execute("SELECT id FROM canonical_ingredients WHERE name='onion'").fetchone()
        if row is None:
            canonical_id = conn.execute(
                "INSERT INTO canonical_ingredients (name, category) "
                "VALUES ('onion', 'vegetable') RETURNING id"
            ).fetchone()[0]
        else:
            canonical_id = row["id"]

    import_url("https://example.com/onion", db_path=db_path, html=html, quiet=True)

    with connect(db_path) as conn:
        ri = conn.execute(
            "SELECT canonical_id FROM recipe_ingredients WHERE original_text='onion'"
        ).fetchone()
        queued = conn.execute(
            "SELECT status, proposed_canonical_id FROM ingredient_mapping_queue "
            "WHERE source='url_import' AND source_key='onion'"
        ).fetchone()
    assert ri["canonical_id"] == canonical_id
    assert queued["status"] == "approved"
    assert queued["proposed_canonical_id"] == canonical_id


def test_import_url_idempotent_enqueue_does_not_duplicate(db_path):
    """Re-importing the same recipe must not duplicate queue rows."""
    html = (FIXTURE_DIR / "seriouseats_pasta_al_tonno.html").read_text(encoding="utf-8")
    import_url(SERIOUS_EATS_URL, db_path=db_path, html=html, quiet=True)
    import_url(SERIOUS_EATS_URL, db_path=db_path, html=html, quiet=True)

    with connect(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM ingredient_mapping_queue WHERE source = 'url_import'"
        ).fetchone()[0]
    assert n == 9


def test_import_url_no_recipe_raises(db_path):
    html = "<html><body>no recipe here</body></html>"
    with pytest.raises(RecipeNotFoundError):
        import_url("https://example.com/nope", db_path=db_path, html=html, quiet=True)


def test_import_url_missing_image_raises_and_does_not_write(db_path):
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Recipe","name":"NoPic","recipeIngredient":["salt"]}'
        "</script>"
    )
    with pytest.raises(RecipeMissingImageError):
        import_url("https://example.com/noimg", db_path=db_path, html=html, quiet=True)
    with connect(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM recipes WHERE source='url'"
        ).fetchone()[0]
    assert n == 0


def test_import_url_blank_image_raises(db_path):
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Recipe","name":"BlankPic","image":"",'
        '"recipeIngredient":["salt"]}'
        "</script>"
    )
    with pytest.raises(RecipeMissingImageError):
        import_url("https://example.com/blank", db_path=db_path, html=html, quiet=True)


def test_import_url_strips_html_in_instructions(db_path):
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Recipe","name":"HTML Test",'
        '"image":"https://example.com/html.jpg",'
        '"recipeInstructions":"<p>Mix <b>well</b>.</p><p>Bake.</p>"}'
        "</script>"
    )
    stats = import_url("https://example.com/htmltest", db_path=db_path, html=html, quiet=True)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT instructions_md FROM recipes WHERE id=?", (stats["recipe_id"],)
        ).fetchone()
    assert row["instructions_md"]
    assert "<p>" not in row["instructions_md"]
    assert "<b>" not in row["instructions_md"]
    assert "Mix well." in row["instructions_md"]
