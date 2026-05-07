"""Generate ``data/seed/demo.jsonl`` from a single live URL.

Fetches https://therecipecritic.com/chicken-fried-rice (override via the
``--url`` arg), extracts the schema.org Recipe entity using the existing
``import_url`` machinery, reshapes it to match the JSONL ingest contract
(see ``docs/jsonl_contract.md``), and writes one JSON line to
``data/seed/demo.jsonl``.

Run once whenever the upstream page changes; the resulting JSONL is what
core ships, not the script.

Usage::

    uv run python scripts/build_demo_jsonl.py
    uv run python scripts/build_demo_jsonl.py --url https://example.com/recipe --source-id my-recipe
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo layout: scripts/<this>.py -> repo root is parent.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pantry_cooking_vibes.importers._nutrition import project_nutrition  # noqa: E402
from pantry_cooking_vibes.importers.url_import import (  # noqa: E402
    RecipeNotFoundError,
    _build_session,
    extract_recipe_jsonld,
    parse_recipe,
)


def _fetch_html_no_brotli(url: str) -> str:
    """Like ``url_import.fetch_html`` but advertises gzip/deflate only.

    The default session asks for ``br`` too, which most modern recipe
    sites prefer to serve. ``requests`` only auto-decompresses Brotli
    when the optional ``brotli`` package is installed; without it the
    response body comes back as undecoded bytes and the JSON-LD regex
    misses everything. Dropping ``br`` from the Accept-Encoding header
    keeps the script self-contained without adding a runtime dep.
    """
    session = _build_session()
    session.headers["Accept-Encoding"] = "gzip, deflate"
    response = session.get(url, timeout=20)
    response.raise_for_status()
    return response.text


DEFAULT_URL = "https://therecipecritic.com/chicken-fried-rice"
DEFAULT_SOURCE_ID = "chicken-fried-rice"
OUT_PATH = REPO_ROOT / "data" / "seed" / "demo.jsonl"


def build_record(url: str, source_id: str) -> dict:
    html = _fetch_html_no_brotli(url)
    entity = extract_recipe_jsonld(html)
    if entity is None:
        raise RecipeNotFoundError(f"no schema.org Recipe found at {url}")

    parsed = parse_recipe(entity, url)

    # parse_recipe() returns a dict shaped for the recipes table (it
    # JSON-serializes nutrition and emits ingredients as bare strings).
    # The JSONL contract wants nutrition as an object and ingredients as
    # objects with at least `original_text`, so unwind those two fields.
    nutrition_dict = project_nutrition(entity.get("nutrition")) or None

    ingredients = [
        {"original_text": text} for text in parsed["ingredients"] if text and text.strip()
    ]

    record: dict = {
        "schema_version": 1,
        "source_id": source_id,
        "name": parsed["name"],
    }
    # Only emit fields with non-null values to keep the demo file tidy.
    for key in ("cooking_time_min", "servings", "instructions_md", "image_url"):
        if parsed.get(key) is not None:
            record[key] = parsed[key]
    if parsed.get("rating") is not None:
        record["rating"] = parsed["rating"]
    if parsed.get("rating_count") is not None:
        record["rating_count"] = parsed["rating_count"]
    if nutrition_dict:
        record["nutrition_json"] = nutrition_dict
    if parsed.get("tags"):
        record["tags"] = parsed["tags"]
    record["ingredients"] = ingredients
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Recipe URL (default: {DEFAULT_URL})")
    ap.add_argument(
        "--source-id",
        default=DEFAULT_SOURCE_ID,
        help=f"Stable id for the recipe (default: {DEFAULT_SOURCE_ID})",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=OUT_PATH,
        help=f"Output path (default: {OUT_PATH})",
    )
    args = ap.parse_args(argv)

    print(f"fetching {args.url}...")
    record = build_record(args.url, args.source_id)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    args.out.write_text(line + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(line)} bytes)")
    print(
        f"  name        : {record['name']}\n"
        f"  ingredients : {len(record['ingredients'])}\n"
        f"  tags        : {len(record.get('tags', []))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
