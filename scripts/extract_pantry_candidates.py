"""Extract a deduplicated, lightly-normalized list of recipe ingredients.

Reads every distinct ``recipe_ingredients.original_text`` from the pantry_cooking_vibes
database, applies minimal normalization (strip parenthesized brand/packaging
text, drop common prep words, lowercase, collapse whitespace), and writes the
sorted unique results to ``data/pantry_candidates/pending.txt``.

Normalization is intentionally simple — it does NOT try to match against
canonical_ingredients. The goal is just to collapse trivial duplicates so a
human reviewer can decide which entries deserve pantry support.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "app.db"
DEFAULT_OUT = PROJECT_ROOT / "data" / "pantry_candidates" / "pending.txt"

# Anything inside parentheses (brand names, pack sizes, sub-brands, etc.)
_PARENS_RE = re.compile(r"\s*\([^)]*\)")

# Prep / state words that change form but not identity. Kept narrow on purpose:
# we want "Shredded Mozzarella" and "Mozzarella" to merge, but not to strip
# descriptors that genuinely distinguish products (organic, baby, etc.).
_PREP_RE = re.compile(
    r"\b(shredded|sliced|diced|chopped|minced|grated|crumbled|"
    r"peeled|cubed|halved|quartered|shaved)\b",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    text = _PARENS_RE.sub("", text)
    text = _PREP_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def extract(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT original_text FROM recipe_ingredients "
            "WHERE original_text IS NOT NULL AND TRIM(original_text) != ''"
        ).fetchall()
    finally:
        conn.close()

    seen: set[str] = set()
    for (raw,) in rows:
        cleaned = normalize(raw)
        if cleaned:
            seen.add(cleaned)
    return sorted(seen)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    items = extract(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(items) + "\n", encoding="utf-8")
    print(f"Wrote {len(items)} unique ingredients to {args.out}")


if __name__ == "__main__":
    main()
