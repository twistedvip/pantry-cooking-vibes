"""Parse pasted URLs and uploaded JSON-LD into import-inbox staging items.

The pure data layer lives in :mod:`import_inbox`; this module is the bridge that
turns raw input (a URL, or the text of a schema.org JSON-LD file) into parsed
``import_items`` and classifies each as ready / review / dup / failed. It reuses
``url_import``'s fetch, JSON-LD extraction, and field coercion so a recipe saved
from the inbox maps to the same shape a single URL import produces.

``build_batch`` runs the parse synchronously; the web layer wraps it in a thread
so a large batch doesn't block the request.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import requests

from pantry_cooking_vibes.db import DB_PATH, connect
from pantry_cooking_vibes.importers import import_inbox, url_import

log = logging.getLogger(__name__)

# (source, source_id) -> recipe id for every library recipe, the dup key.
ExistingKeys = dict[tuple[str, str], int]


def _existing_keys(conn: Any) -> ExistingKeys:
    rows = conn.execute(
        "SELECT source, source_id, id FROM recipes WHERE source_id IS NOT NULL"
    ).fetchall()
    return {(r["source"], r["source_id"]): r["id"] for r in rows}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _failed_item(source: str, source_id: str | None, name: str | None, reason: str) -> dict:
    return {
        "status": "failed",
        "source": source,
        "source_id": source_id,
        "name": name,
        "image_url": None,
        "ingredients": [],
        "tags": [],
        "failure_reason": reason,
    }


def _build_item(source: str, rec: dict, existing: ExistingKeys) -> dict:
    """Turn a parsed-recipe dict into a staged item with a triage status.

    Classification order (most blocking first): no title -> no image (both
    can't enter the library) -> duplicate of a library recipe -> thin parse
    (missing ingredients or steps -> review) -> ready.
    """
    name = (rec.get("name") or "").strip()
    image = (rec.get("image_url") or "").strip()
    source_id = rec.get("source_id")

    item = {
        "source": source,
        "source_id": source_id,
        "name": name or None,
        "cooking_time_min": rec.get("cooking_time_min"),
        "servings": rec.get("servings"),
        "instructions_md": rec.get("instructions_md"),
        "nutrition_json": rec.get("nutrition_json"),
        "image_url": image or None,
        "rating": rec.get("rating"),
        "rating_count": rec.get("rating_count"),
        "ingredients": rec.get("ingredients") or [],
        "tags": rec.get("tags") or [],
    }

    if not name or name == "(untitled)":
        item.update(status="failed", name=None, failure_reason="no title parsed")
        return item
    if not image:
        item.update(status="failed", failure_reason="no image")
        return item
    dup_id = existing.get((source, source_id)) if source_id else None
    if dup_id is not None:
        item.update(status="dup", dup_of_recipe_id=dup_id)
        return item
    if not item["ingredients"]:
        item.update(status="review", review_reason="no ingredients parsed")
        return item
    if not item["instructions_md"]:
        item.update(status="review", review_reason="no steps parsed")
        return item
    item["status"] = "ready"
    return item


def parse_url(url: str, *, existing: ExistingKeys, html: str | None = None) -> dict:
    """Fetch and parse one URL into a staged item. Network failures and pages
    with no schema.org Recipe become ``failed`` items rather than raising, so a
    bad URL never sinks the whole batch."""
    try:
        page = html if html is not None else url_import.fetch_html(url)
    except url_import.UnsafeURLError:
        return _failed_item("url", url, None, "points to a private or non-public address")
    except requests.RequestException:
        return _failed_item("url", url, None, "couldn't reach the page")
    entity = url_import.extract_recipe_jsonld(page)
    if entity is None:
        return _failed_item("url", url, None, "no recipe data on the page")
    rec = url_import.parse_recipe(entity, url)
    return _build_item("url", rec, existing)


def _entity_url(entity: dict) -> str:
    for key in ("url", "@id", "mainEntityOfPage"):
        val = entity.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            inner = val.get("url") or val.get("@id")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def parse_jsonld_text(text: str, *, existing: ExistingKeys) -> list[dict]:
    """Parse the text of a schema.org JSON-LD file into staged items.

    Handles a single Recipe object, an array, or a ``@graph`` wrapper. A recipe
    with its own URL dedups against URL imports; one without gets a synthesized
    ``jsonld:`` key so re-importing the same file is idempotent.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [_failed_item("jsonld", None, None, "file is not valid JSON")]

    items: list[dict] = []
    for entity in url_import._iter_entities(data):
        if not url_import._is_recipe(entity):
            continue
        url = _entity_url(entity)
        if url.startswith("http://") or url.startswith("https://"):
            source, source_id = "url", url
        else:
            name = url_import._text(entity.get("name") or entity.get("headline")) or "untitled"
            source, source_id = "jsonld", f"jsonld:{_slug(name)}"
        rec = url_import.parse_recipe(entity, source_id)
        items.append(_build_item(source, rec, existing))

    if not items:
        items.append(_failed_item("jsonld", None, None, "no recipes found in the file"))
    return items


def build_batch(
    *,
    urls: list[str] | None = None,
    file_text: str | None = None,
    file_name: str | None = None,
    label: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Parse the given URLs and/or JSON-LD file into a new batch. Returns its id.

    Synchronous: the web layer runs this in a background thread and polls the
    batch's parse progress.
    """
    db = db_path or DB_PATH
    urls = [u.strip() for u in (urls or []) if u.strip()]
    with connect(db) as conn:
        existing = _existing_keys(conn)

    items: list[dict] = []
    parts: list[str] = []
    if file_text is not None:
        items.extend(parse_jsonld_text(file_text, existing=existing))
        parts.append(file_name or "uploaded file")
    for url in urls:
        items.append(parse_url(url, existing=existing))
    if urls:
        parts.append(f"{len(urls)} URL" + ("" if len(urls) == 1 else "s"))

    batch_label = label or " + ".join(parts) or "import"
    batch_id = import_inbox.create_batch(batch_label, total=len(items), db_path=db)
    for item in items:
        import_inbox.add_item(batch_id, item, db_path=db)
    import_inbox.finish_batch(batch_id, db_path=db)
    return batch_id
