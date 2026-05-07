"""One-shot DB cleanup for the pantry-cooking-vibes. Safe to re-run.

Steps (in order):
  1. Drop ``recipes.raw_json`` and ``recipes.slug`` (+ its index) if present.
     These were ~90% of total DB size and are derivable / archived elsewhere.
  2. Prune ``recipe_tags`` rows where tag appears on >50% of recipes
     (negative-space tags like 'caffeine-free', 'has garlic').
  3. Rewrite ``recipes.nutrition_json`` from HR's verbose 30-field dict to a
     compact 6-field macro dict. Idempotent via ``project_nutrition``.
  4. ``VACUUM`` to reclaim freed pages.

Usage:
    .venv/Scripts/python.exe scripts/cleanup_db.py [db_path]
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from pantry_cooking_vibes.db import DB_PATH  # noqa: E402
from pantry_cooking_vibes.importers._nutrition import project_nutrition  # noqa: E402

BATCH = 1000


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def drop_bloat_columns(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "recipes")
    if "raw_json" in cols:
        print("  dropping recipes.raw_json ...")
        conn.execute("ALTER TABLE recipes DROP COLUMN raw_json")
    else:
        print("  recipes.raw_json already absent — skip")
    if "slug" in cols:
        print("  dropping idx_recipes_slug + recipes.slug ...")
        conn.execute("DROP INDEX IF EXISTS idx_recipes_slug")
        conn.execute("ALTER TABLE recipes DROP COLUMN slug")
    else:
        print("  recipes.slug already absent — skip")
    conn.commit()


def prune_noisy_tags(conn: sqlite3.Connection) -> int:
    before = conn.execute("SELECT COUNT(*) FROM recipe_tags").fetchone()[0]
    noisy = [
        r[0]
        for r in conn.execute(
            """
            SELECT tag
            FROM recipe_tags
            GROUP BY tag
            HAVING COUNT(*) > (SELECT COUNT(*) FROM recipes) / 2
            """
        )
    ]
    if not noisy:
        print("  no tags exceed the 50% threshold — skip")
        return 0
    print(
        f"  pruning {len(noisy)} high-frequency tags: {noisy[:8]}{'...' if len(noisy) > 8 else ''}"
    )
    conn.executemany("DELETE FROM recipe_tags WHERE tag = ?", [(t,) for t in noisy])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM recipe_tags").fetchone()[0]
    print(f"  recipe_tags rows: {before:,} -> {after:,} (-{before - after:,})")
    return before - after


def normalize_nutrition(conn: sqlite3.Connection) -> None:
    total = conn.execute(
        "SELECT COUNT(*) FROM recipes WHERE nutrition_json IS NOT NULL"
    ).fetchone()[0]
    if total == 0:
        print("  no nutrition_json rows — skip")
        return
    print(f"  rewriting nutrition_json on {total:,} rows ...")
    cur = conn.execute("SELECT id, nutrition_json FROM recipes WHERE nutrition_json IS NOT NULL")
    buf: list[tuple[str | None, int]] = []
    rewritten = 0
    for row in cur:
        try:
            src = json.loads(row[1])
        except json.JSONDecodeError:
            src = None
        compact = project_nutrition(src)
        new_val = json.dumps(compact, ensure_ascii=False) if compact else None
        buf.append((new_val, row[0]))
        if len(buf) >= BATCH:
            conn.executemany("UPDATE recipes SET nutrition_json = ? WHERE id = ?", buf)
            conn.commit()
            rewritten += len(buf)
            buf.clear()
            print(f"    ...{rewritten:,} / {total:,}")
    if buf:
        conn.executemany("UPDATE recipes SET nutrition_json = ? WHERE id = ?", buf)
        conn.commit()
        rewritten += len(buf)
    print(f"  rewrote {rewritten:,} rows")


def vacuum(conn: sqlite3.Connection) -> None:
    print("  running VACUUM (can take several minutes on a large DB) ...")
    t0 = time.monotonic()
    conn.isolation_level = None  # VACUUM can't run inside a transaction
    conn.execute("VACUUM")
    print(f"  VACUUM done in {time.monotonic() - t0:.1f}s")


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    if not db.exists():
        print(f"db not found: {db}", file=sys.stderr)
        sys.exit(1)
    size_before = db.stat().st_size / 1024 / 1024
    print(f"DB: {db} ({size_before:,.1f} MB)")

    conn = sqlite3.connect(str(db))
    try:
        print("\n[1/4] schema — drop raw_json / slug")
        drop_bloat_columns(conn)
        print("\n[2/4] recipe_tags — prune >50% tags")
        prune_noisy_tags(conn)
        print("\n[3/4] nutrition_json — compact")
        normalize_nutrition(conn)
        print("\n[4/4] VACUUM")
        vacuum(conn)
    finally:
        conn.close()

    size_after = db.stat().st_size / 1024 / 1024
    print(
        f"\nBefore: {size_before:,.1f} MB  After: {size_after:,.1f} MB  "
        f"Reclaimed: {size_before - size_after:,.1f} MB "
        f"({(1 - size_after / size_before) * 100:.1f}%)"
    )


if __name__ == "__main__":
    main()
