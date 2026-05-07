"""Ingredient normalizer: fuzzy-match raw product strings against canonical_ingredients."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from pantry_cooking_vibes.db import connect, fetchall

log = logging.getLogger(__name__)

# Brand/packaging words stripped before matching
_STRIP_WORDS = re.compile(
    r"\b(organic|fresh|frozen|raw|cooked|dried|diced|sliced|chopped|minced|"
    r"shredded|whole|baby|mini|large|small|medium|extra|lean|boneless|skinless|"
    r"grass.fed|free.range|cage.free|wild.caught|farm.raised|"
    r"bag|pack|box|container|jar|can|bottle|bunch|head|stalk|"
    r"florets?|strips?|fillets?|pieces?|oz|lb|g|kg|ml|cup|tbsp|tsp)\b",
    re.IGNORECASE,
)

AUTO_APPROVE_THRESHOLD = 90.0  # score ≥ this → auto-approve
PROPOSE_THRESHOLD = 70.0  # score < this → no_match (confidence=0). 70 is the
# rapidfuzz WRatio review-queue rule of thumb; below
# that, hits are mostly noise that wastes reviewer time.


@dataclass
class NormalizationResult:
    source_key: str
    original_text: str
    proposed_canonical_id: int | None
    confidence: float
    status: str  # 'approved' | 'proposed' | 'no_match'


def _fold_unicode(s: str) -> str:
    """NFKD normalize and strip combining marks so 'jalapeño' folds to 'jalapeno'."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _clean(text: str) -> str:
    """Strip brand/packaging words, ASCII-fold accents, and normalize whitespace."""
    folded = _fold_unicode(text)
    cleaned = _STRIP_WORDS.sub(" ", folded)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _load_index(conn: sqlite3.Connection) -> list[dict]:
    """Load canonical ingredients into an in-memory index with expanded aliases."""
    rows = fetchall(conn, "SELECT id, name, aliases FROM canonical_ingredients ORDER BY id")
    index = []
    for row in rows:
        aliases_raw = row["aliases"] or "[]"
        # Support both JSON arrays and pipe-separated strings (legacy seed format)
        if aliases_raw.startswith("["):
            try:
                aliases = json.loads(aliases_raw)
            except json.JSONDecodeError:
                aliases = []
        else:
            aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()]

        index.append(
            {
                "id": row["id"],
                "name": row["name"],
                "aliases": aliases,
                # All searchable strings for this canonical
                "choices": [row["name"]] + aliases,
            }
        )
    return index


def _build_choice_map(index: list[dict]) -> tuple[list[str], dict[str, int]]:
    """Return (choices_list, choice→canonical_id mapping) for rapidfuzz.

    First-write-wins: if two canonicals share an alias, the lower id keeps it
    (index is loaded ORDER BY id). Collisions are logged so curators can dedup
    the seed.
    """
    choices: list[str] = []
    choice_to_id: dict[str, int] = {}
    for entry in index:
        for choice in entry["choices"]:
            key = _fold_unicode(choice).lower()
            existing = choice_to_id.get(key)
            if existing is None:
                choices.append(key)
                choice_to_id[key] = entry["id"]
            elif existing != entry["id"]:
                log.warning(
                    "alias collision: %r already mapped to canonical %d, "
                    "ignoring conflicting mapping to %d",
                    key,
                    existing,
                    entry["id"],
                )
    return choices, choice_to_id


def match_one(
    text: str, choices: list[str], choice_to_id: dict[str, int]
) -> tuple[int | None, float]:
    """Fuzzy-match *text* against all canonical choices. Returns (canonical_id, score)."""
    cleaned = _clean(text)
    if not cleaned:
        return None, 0.0

    result = process.extractOne(
        cleaned, choices, scorer=fuzz.WRatio, score_cutoff=PROPOSE_THRESHOLD
    )
    if result is None:
        return None, 0.0

    best_match, score, _ = result
    canonical_id = choice_to_id[best_match]
    return canonical_id, float(score)


def normalize_text(
    text: str, choices: list[str], choice_to_id: dict[str, int]
) -> NormalizationResult:
    """Produce a NormalizationResult for a single raw ingredient string."""
    canonical_id, score = match_one(text, choices, choice_to_id)

    if score >= AUTO_APPROVE_THRESHOLD and canonical_id is not None:
        status = "approved"
    elif canonical_id is not None:
        status = "proposed"
    else:
        status = "no_match"

    return NormalizationResult(
        source_key=text,
        original_text=text,
        proposed_canonical_id=canonical_id,
        confidence=round(score / 100.0, 4),
        status=status,
    )


def upsert_mapping(
    conn: sqlite3.Connection,
    source: str,
    result: NormalizationResult,
    *,
    overwrite: bool = False,
) -> None:
    """Insert or ignore a mapping queue row. With overwrite=True, updates existing rows."""
    db_status = result.status if result.status != "no_match" else "proposed"
    if overwrite:
        conn.execute(
            """
            INSERT INTO ingredient_mapping_queue
                (source, source_key, original_text, proposed_canonical_id, confidence, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_key) DO UPDATE SET
                original_text        = excluded.original_text,
                proposed_canonical_id = excluded.proposed_canonical_id,
                confidence           = excluded.confidence,
                status               = excluded.status
            """,
            (
                source,
                result.source_key,
                result.original_text,
                result.proposed_canonical_id,
                result.confidence,
                db_status,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO ingredient_mapping_queue
                (source, source_key, original_text, proposed_canonical_id, confidence, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_key) DO NOTHING
            """,
            (
                source,
                result.source_key,
                result.original_text,
                result.proposed_canonical_id,
                result.confidence,
                db_status,
            ),
        )


def run_normalization(
    source: str,
    items: list[dict],  # list of {source_key: str, original_text: str}
    *,
    db_path: Path | None = None,
    overwrite: bool = False,
    quiet: bool = False,
) -> dict:
    """
    Normalize a batch of items against the canonical ingredient index.

    Args:
        source: source tag (free-form, e.g. an importer-specific slug)
        items: list of dicts with 'source_key' and 'original_text'
        db_path: override default DB path
        overwrite: re-process already-queued items
        quiet: suppress progress output

    Returns:
        stats dict with keys: total, approved, proposed, no_match
    """
    from pantry_cooking_vibes.db import DB_PATH

    path = db_path or DB_PATH

    stats = {"total": 0, "approved": 0, "proposed": 0, "no_match": 0}

    with connect(path) as conn:
        index = _load_index(conn)
        choices, choice_to_id = _build_choice_map(index)

        for item in items:
            source_key = item["source_key"]
            original_text = item["original_text"]

            result = normalize_text(original_text, choices, choice_to_id)
            result.source_key = source_key  # use the canonical key, not text

            upsert_mapping(conn, source, result, overwrite=overwrite)

            stats["total"] += 1
            if result.status == "approved":
                stats["approved"] += 1
            elif result.status == "proposed":
                stats["proposed"] += 1
            else:
                stats["no_match"] += 1

            if not quiet and stats["total"] % 100 == 0:
                print(f"  normalized {stats['total']} / {len(items)} ...")

    return stats


RECIPE_TEXT_SOURCE = "recipe_ingredient_text"


def backfill_recipe_canonicals(*, db_path: Path | None = None, quiet: bool = False) -> dict:
    """Backfill ``recipe_ingredients.canonical_id`` directly from distinct
    ``original_text`` values via the same fuzzy matcher used by ``run_normalization``.

    Auto-writes when the match clears ``AUTO_APPROVE_THRESHOLD`` (≥90).
    Proposed (70–89) and no_match texts are queued in
    ``ingredient_mapping_queue`` under source ``recipe_ingredient_text`` so
    they show up in ``review-mappings``. Never overwrites a row that already
    has a ``canonical_id``.
    """
    from pantry_cooking_vibes.db import DB_PATH

    path = db_path or DB_PATH

    stats = {
        "distinct_texts": 0,
        "approved": 0,
        "proposed": 0,
        "no_match": 0,
        "rows_updated": 0,
    }

    with connect(path) as conn:
        index = _load_index(conn)
        choices, choice_to_id = _build_choice_map(index)

        texts = [
            r["original_text"]
            for r in conn.execute(
                """
                SELECT DISTINCT original_text
                FROM recipe_ingredients
                WHERE canonical_id IS NULL
                  AND original_text IS NOT NULL
                  AND original_text <> ''
                """
            ).fetchall()
        ]

        for text in texts:
            stats["distinct_texts"] += 1
            result = normalize_text(text, choices, choice_to_id)

            if result.status == "approved" and result.proposed_canonical_id is not None:
                cur = conn.execute(
                    """
                    UPDATE recipe_ingredients
                    SET canonical_id = ?
                    WHERE canonical_id IS NULL
                      AND original_text = ?
                    """,
                    (result.proposed_canonical_id, text),
                )
                stats["rows_updated"] += cur.rowcount
                stats["approved"] += 1
            elif result.status == "proposed":
                stats["proposed"] += 1
                upsert_mapping(conn, RECIPE_TEXT_SOURCE, result, overwrite=False)
            else:
                stats["no_match"] += 1
                upsert_mapping(conn, RECIPE_TEXT_SOURCE, result, overwrite=False)

            if not quiet and stats["distinct_texts"] % 500 == 0:
                print(
                    f"  scanned {stats['distinct_texts']} distinct texts "
                    f"(updated {stats['rows_updated']} rows)..."
                )

    return stats


def apply_text_mappings(*, db_path: Path | None = None, quiet: bool = False) -> dict:
    """Apply approved ``recipe_ingredient_text`` queue rows back to
    ``recipe_ingredients.canonical_id`` (matched on ``source_key`` ==
    ``original_text``). Idempotent. Skips rows already mapped."""
    from pantry_cooking_vibes.db import DB_PATH

    path = db_path or DB_PATH
    stats = {"approved_queue_rows": 0, "rows_updated": 0}

    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT source_key, proposed_canonical_id
            FROM ingredient_mapping_queue
            WHERE source = ?
              AND status = 'approved'
              AND proposed_canonical_id IS NOT NULL
            """,
            (RECIPE_TEXT_SOURCE,),
        ).fetchall()
        for row in rows:
            stats["approved_queue_rows"] += 1
            cur = conn.execute(
                """
                UPDATE recipe_ingredients
                SET canonical_id = ?
                WHERE canonical_id IS NULL
                  AND original_text = ?
                """,
                (row["proposed_canonical_id"], row["source_key"]),
            )
            stats["rows_updated"] += cur.rowcount
            if not quiet and stats["approved_queue_rows"] % 200 == 0:
                print(
                    f"  applied {stats['approved_queue_rows']} approvals "
                    f"({stats['rows_updated']} rows)..."
                )
    return stats


def review_pending(db_path: Path | None = None, limit: int = 50) -> list[dict]:
    """Return pending proposed/no_match rows for human review."""
    from pantry_cooking_vibes.db import DB_PATH

    path = db_path or DB_PATH
    with connect(path) as conn:
        rows = fetchall(
            conn,
            """
            SELECT q.id, q.source, q.source_key, q.original_text,
                   q.proposed_canonical_id, q.confidence, q.status,
                   c.name AS canonical_name
            FROM ingredient_mapping_queue q
            LEFT JOIN canonical_ingredients c ON c.id = q.proposed_canonical_id
            WHERE q.status IN ('proposed', 'no_match')
            ORDER BY q.confidence DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]


def approve_mapping(queue_id: int, canonical_id: int, db_path: Path | None = None) -> None:
    """Approve a queued mapping, optionally overriding the proposed canonical."""
    from pantry_cooking_vibes.db import DB_PATH

    path = db_path or DB_PATH
    with connect(path) as conn:
        conn.execute(
            """
            UPDATE ingredient_mapping_queue
            SET status='approved', proposed_canonical_id=?
            WHERE id=?
            """,
            (canonical_id, queue_id),
        )


def reject_mapping(queue_id: int, db_path: Path | None = None) -> None:
    """Reject a queued mapping."""
    from pantry_cooking_vibes.db import DB_PATH

    path = db_path or DB_PATH
    with connect(path) as conn:
        conn.execute(
            "UPDATE ingredient_mapping_queue SET status='rejected' WHERE id=?",
            (queue_id,),
        )
