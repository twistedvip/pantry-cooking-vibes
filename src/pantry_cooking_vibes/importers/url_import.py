"""Import a recipe from a URL by extracting schema.org Recipe JSON-LD.

Public entry point: ``import_url(url, *, db_path=None, html=None, quiet=False)``.
Pass ``html=`` to skip the network fetch (used by tests and for replaying
captured pages).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pantry_cooking_vibes.db import DB_PATH, connect
from pantry_cooking_vibes.importers._nutrition import project_nutrition
from pantry_cooking_vibes.importers._utils import _html_to_text, _to_float, _to_int

log = logging.getLogger(__name__)

# Major recipe sites (Dotdash Meredith, Condé Nast, etc.) 403 unfamiliar UAs,
# so we send a realistic desktop-browser fingerprint. Override via
# PANTRY_COOKING_VIBES_UA env var if a specific site needs something different.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
USER_AGENT = os.environ.get("PANTRY_COOKING_VIBES_UA", _DEFAULT_UA)
REQUEST_TIMEOUT = 20

_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_ISO8601_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


# ---------- HTTP ----------


def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return s


def fetch_html(url: str, *, session: requests.Session | None = None) -> str:
    """Fetch HTML for *url*. Raises ``requests.HTTPError`` on non-2xx."""
    s = session or _build_session()
    r = s.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


# ---------- JSON-LD extraction ----------


def _iter_entities(obj: Any) -> Iterable[dict]:
    """Walk a JSON-LD value yielding every dict-shaped entity (handles @graph)."""
    if isinstance(obj, dict):
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_entities(item)
        else:
            yield obj
            for v in obj.values():
                if isinstance(v, (dict | list)):
                    yield from _iter_entities(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_entities(item)


def _is_recipe(entity: dict) -> bool:
    t = entity.get("@type")
    if isinstance(t, str):
        return t == "Recipe"
    if isinstance(t, list):
        return "Recipe" in t
    return False


def extract_recipe_jsonld(html: str) -> dict | None:
    """Find the first schema.org Recipe entity in the page's JSON-LD blocks."""
    for raw in _JSONLD_RE.findall(html):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            log.warning("skipping malformed JSON-LD block")
            continue
        for entity in _iter_entities(data):
            if _is_recipe(entity):
                return entity
    return None


# ---------- field coercion ----------


def parse_iso_duration(value: Any) -> int | None:
    """Convert an ISO 8601 duration like 'PT1H30M' to whole minutes."""
    if not isinstance(value, str) or not value:
        return None
    m = _ISO8601_RE.match(value.strip())
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict(default="0").items()}
    minutes = parts["days"] * 24 * 60 + parts["hours"] * 60 + parts["minutes"]
    if parts["seconds"] >= 30:
        minutes += 1
    return minutes


def parse_yield(value: Any) -> int | None:
    """schema.org recipeYield is variable: int, str, or list of str."""
    if value is None:
        return None
    if isinstance(value, list):
        for v in value:
            n = parse_yield(v)
            if n is not None:
                return n
        return None
    if isinstance(value, (int | float)):
        n = int(value)
        return n if n >= 1 else None
    if isinstance(value, str):
        m = re.search(r"\d+", value)
        if m:
            n = int(m.group(0))
            return n if n >= 1 else None
    return None


def _image_url(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("url") or None
    if isinstance(value, list):
        for v in value:
            url = _image_url(v)
            if url:
                return url
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [s for s in (_text(v) for v in value) if s]
        return ", ".join(parts) or None
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("text"))
    return None


def _rating(entity: dict) -> tuple[float | None, int | None]:
    agg = entity.get("aggregateRating") or {}
    if not isinstance(agg, dict):
        return None, None
    return _to_float(agg.get("ratingValue")), _to_int(
        agg.get("ratingCount") or agg.get("reviewCount")
    )


def _normalize_bulleted(text: str | None) -> str | None:
    """Some sites pack multiple `•` bullets into a single HowToStep separated
    by soft-wrap newlines. Split on `•`, collapse intra-bullet whitespace, and
    re-join with single `\\n` so each bullet renders as its own step."""
    if not text or "•" not in text:
        return text
    bullets = [re.sub(r"\s+", " ", p).strip() for p in text.split("•")]
    return "\n".join(b for b in bullets if b) or None


def _instructions(value: Any) -> str | None:
    """Render schema.org recipeInstructions to plain markdown-ish text."""
    if value is None:
        return None
    if isinstance(value, str):
        return _normalize_bulleted(_html_to_text(value))
    if isinstance(value, dict):
        # HowToSection or single HowToStep
        if value.get("@type") == "HowToSection":
            header = (value.get("name") or "").strip()
            body = _instructions(value.get("itemListElement"))
            if header and body:
                return f"## {header}\n{body}"
            return body or (header or None)
        return _normalize_bulleted(_html_to_text(value.get("text") or value.get("name")))
    if isinstance(value, list):
        steps = [s for s in (_instructions(v) for v in value) if s]
        return "\n\n".join(steps) or None
    return None


def _collect_tags(entity: dict) -> list[str]:
    raw: list[str] = []
    for key in ("keywords", "recipeCategory", "recipeCuisine"):
        v = entity.get(key)
        if isinstance(v, str):
            raw.extend(t.strip() for t in v.split(","))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    raw.append(item.strip())
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        norm = t.lower()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def parse_recipe(entity: dict, url: str) -> dict:
    """Map a schema.org Recipe entity to our recipes-row dict shape."""
    cooking_time = parse_iso_duration(entity.get("totalTime")) or parse_iso_duration(
        entity.get("cookTime")
    )
    rating, rating_count = _rating(entity)
    nutrition = project_nutrition(entity.get("nutrition"))

    return {
        "source_id": url,
        "name": _text(entity.get("name") or entity.get("headline")) or "(untitled)",
        "cooking_time_min": cooking_time,
        "servings": parse_yield(entity.get("recipeYield")),
        "instructions_md": _instructions(entity.get("recipeInstructions")),
        "nutrition_json": json.dumps(nutrition, ensure_ascii=False) if nutrition else None,
        "image_url": _image_url(entity.get("image")),
        "rating": rating,
        "rating_count": rating_count,
        "ingredients": [
            s.strip()
            for s in (entity.get("recipeIngredient") or [])
            if isinstance(s, str) and s.strip()
        ],
        "tags": _collect_tags(entity),
    }


# ---------- DB writes ----------


def _load_canonical_map(conn: sqlite3.Connection) -> dict[str, int]:
    """Map raw ingredient text -> canonical_id from the URL-import review queue.

    Only reads ``status='approved'`` rows so ingest never wires
    recipe_ingredients to a canonical the curator hasn't approved.
    """
    rows = conn.execute(
        """
        SELECT source_key, proposed_canonical_id
        FROM ingredient_mapping_queue
        WHERE source = 'url_import'
          AND status = 'approved'
          AND proposed_canonical_id IS NOT NULL
        """
    ).fetchall()
    return {r["source_key"]: r["proposed_canonical_id"] for r in rows}


def _enqueue_ingredients(
    conn: sqlite3.Connection,
    ingredients: list[str],
    canonical_map: dict[str, int],
) -> dict[str, int]:
    """Run the normalizer on each ingredient string and upsert into the
    ``ingredient_mapping_queue`` under ``source='url_import'``. Mirrors the
    HR-products flow so URL-imported recipes don't silently land with
    ``canonical_id=NULL``. Returns an updated map including newly
    auto-approved (or proposed) hits."""
    from pantry_cooking_vibes.importers.normalize import (
        _build_choice_map,
        _load_index,
        normalize_text,
        upsert_mapping,
    )

    out = dict(canonical_map)
    distinct = {t for t in ingredients if t and t.strip()}
    if not distinct:
        return out

    index = _load_index(conn)
    choices, choice_to_id = _build_choice_map(index)
    for text in distinct:
        if text in out:
            continue
        result = normalize_text(text, choices, choice_to_id)
        upsert_mapping(conn, "url_import", result, overwrite=False)
        # Only apply auto-approved hits (>=85% confidence). Proposed (70-84%) and
        # no_match rows wait for human review before being wired to recipe_ingredients.
        if result.proposed_canonical_id is not None and result.status == "approved":
            out[text] = result.proposed_canonical_id
    return out


def _upsert_recipe(conn: sqlite3.Connection, rec: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO recipes
            (source, source_id, name, cooking_time_min, servings,
             instructions_md, nutrition_json, image_url, rating, rating_count)
        VALUES ('url', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            name             = excluded.name,
            cooking_time_min = excluded.cooking_time_min,
            servings         = excluded.servings,
            instructions_md  = excluded.instructions_md,
            nutrition_json   = excluded.nutrition_json,
            image_url        = excluded.image_url,
            rating           = excluded.rating,
            rating_count     = excluded.rating_count
        RETURNING id
        """,
        (
            rec["source_id"],
            rec["name"],
            rec["cooking_time_min"],
            rec["servings"],
            rec["instructions_md"],
            rec["nutrition_json"],
            rec["image_url"],
            rec["rating"],
            rec["rating_count"],
        ),
    )
    return cur.fetchone()["id"]


def _replace_tags(conn: sqlite3.Connection, recipe_id: int, tags: list[str]) -> int:
    conn.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (recipe_id,))
    inserted = 0
    for tag in tags:
        conn.execute(
            "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag) VALUES (?, ?)",
            (recipe_id, tag),
        )
        inserted += 1
    return inserted


def _replace_ingredients(
    conn: sqlite3.Connection,
    recipe_id: int,
    ingredients: list[str],
    canonical_map: dict[str, int],
) -> int:
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    for text in ingredients:
        conn.execute(
            """
            INSERT INTO recipe_ingredients
                (recipe_id, canonical_id, original_text, quantity, unit, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (recipe_id, canonical_map.get(text), text, None, None, None),
        )
    return len(ingredients)


# ---------- public entry point ----------


class RecipeNotFoundError(ValueError):
    """Raised when no schema.org Recipe is present in the page."""


class RecipeMissingImageError(ValueError):
    """Raised when a parsed recipe has no usable image URL."""


def import_url(
    url: str,
    *,
    db_path: Path | None = None,
    html: str | None = None,
    quiet: bool = False,
) -> dict:
    """Import a single recipe page. Returns a stats dict."""
    db = db_path or DB_PATH
    page = html if html is not None else fetch_html(url)
    entity = extract_recipe_jsonld(page)
    if entity is None:
        raise RecipeNotFoundError(f"no schema.org Recipe found at {url}")

    rec = parse_recipe(entity, url)
    img = rec.get("image_url")
    if not img or not img.strip():
        raise RecipeMissingImageError(f"recipe at {url} has no image; skipping import")

    with connect(db) as conn:
        canonical_map = _load_canonical_map(conn)
        canonical_map = _enqueue_ingredients(conn, rec["ingredients"], canonical_map)
        recipe_id = _upsert_recipe(conn, rec)
        tag_count = _replace_tags(conn, recipe_id, rec["tags"])
        ing_count = _replace_ingredients(conn, recipe_id, rec["ingredients"], canonical_map)

    if not quiet:
        log.info("imported recipe id=%d name=%r", recipe_id, rec["name"])

    return {
        "recipe_id": recipe_id,
        "name": rec["name"],
        "ingredients": ing_count,
        "tags": tag_count,
    }
