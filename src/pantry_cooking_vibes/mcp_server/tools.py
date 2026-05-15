"""Pure-function MCP tools for the meal planner.

Each function takes an optional ``db_path`` so it's testable without spinning
up an MCP server. The thin ``server`` module imports and wraps these.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pantry_cooking_vibes.dates import current_sunday
from pantry_cooking_vibes.db import DB_PATH, connect

log = logging.getLogger(__name__)

DEFAULT_RESULT_LIMIT = 20
MAX_RESULT_LIMIT = 250
_DAY_VALUES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_MEAL_SLOT_VALUES = {"breakfast", "lunch", "dinner"}
_WEEK_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_RESULT_LIMIT))


# ---------- Recipe search & detail ----------


def _fts5_escape_query(query: str) -> str:
    """Turn user input into a safe FTS5 MATCH expression.

    FTS5 reserves ``-`` (NOT), ``:`` (column filter), ``*`` (prefix),
    ``"`` (phrase), and ``()`` (grouping). A bare query like
    ``"One-Pan Chicken"`` is parsed as ``One NOT Pan ...`` and surfaces
    a confusing ``no such column: Pan`` OperationalError. Wrapping each
    whitespace-separated token in double quotes makes FTS5 treat it as
    a literal phrase and neutralizes every operator inside. Embedded
    double quotes are escaped by doubling, per the FTS5 spec.
    """
    tokens = query.split()
    if not tokens:
        return ""
    return " ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens)


def _resolve_ingredient_canonical_ids(
    conn: sqlite3.Connection, names: list[str]
) -> list[list[int]]:
    """Map each ingredient name → matching canonical_ids (case-insensitive substring).

    Returns one id-group per non-blank input name. Matching is by substring, so
    ``"chicken"`` resolves to every canonical containing it (``chicken breast``,
    ``chicken thigh``, ...) — canonical names are specific, users aren't. A name
    with no matches yields an empty group; callers treat an empty group as
    "unsatisfiable." An empty input list returns ``[]`` ("no ingredient filter").
    """
    groups: list[list[int]] = []
    for raw in names:
        term = raw.strip().lower()
        if not term:
            continue
        # Escape LIKE wildcards in user input so they match literally.
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE LOWER(name) LIKE ? ESCAPE '\\'",
            (like,),
        ).fetchall()
        groups.append([r["id"] for r in rows])
    return groups


def _canonical_id_exists_clause(ids: list[int]) -> str:
    """SQL ``EXISTS`` fragment: recipe ``r`` has an ingredient with a canonical_id in ``ids``.

    The caller must ``params.extend(ids)`` in the same order.
    """
    placeholders = ",".join("?" * len(ids))  # "?,?,?" from int len → safe to interpolate
    return (
        f"EXISTS (SELECT 1 FROM recipe_ingredients ri "  # noqa: S608
        f"WHERE ri.recipe_id = r.id AND ri.canonical_id IN ({placeholders}))"
    )


def search_recipes(
    query: str = "",
    max_time_min: int | None = None,
    tags: list[str] | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
    favorites_only: bool = False,
    sources: list[str] | None = None,
    ingredients: list[str] | None = None,
    ingredient_mode: str = "and",
    pantry_only: bool = False,
    *,
    db_path: Path | None = None,
) -> list[dict]:
    """Search recipes via FTS5 + cooking-time + tag + source filters.

    Empty query browses all recipes ordered by rating. Empty/None ``sources``
    means no source restriction.

    ``ingredients`` filters to recipes containing the named canonical ingredients.
    ``ingredient_mode='and'`` requires all of them; ``'or'`` requires any.
    ``pantry_only=True`` further restricts to recipes whose mapped ingredients
    are all present in the pantry (unmapped ingredients are ignored).
    """
    db = db_path or DB_PATH
    limit = _clamp_limit(limit)
    if ingredient_mode not in ("and", "or"):
        raise ValueError("ingredient_mode must be 'and' or 'or'")
    params: list[Any] = []
    where: list[str] = []

    select_cols = (
        "SELECT r.id, r.source, r.name, r.cooking_time_min, r.servings, "
        "r.rating, r.rating_count, r.image_url, "
        "(rf.recipe_id IS NOT NULL) AS is_favorite "
    )
    if query.strip():
        sql_base = (
            f"{select_cols}"
            "FROM recipes_fts f JOIN recipes r ON r.id = f.rowid "
            "LEFT JOIN recipe_favorites rf ON rf.recipe_id = r.id "
            "WHERE recipes_fts MATCH ?"
        )
        params.append(_fts5_escape_query(query))
        order = "ORDER BY f.rank, r.rating DESC NULLS LAST"
    else:
        sql_base = (
            f"{select_cols}"
            "FROM recipes r "
            "LEFT JOIN recipe_favorites rf ON rf.recipe_id = r.id "
            "WHERE 1=1"
        )
        order = "ORDER BY is_favorite DESC, r.rating DESC NULLS LAST, r.id"

    if max_time_min is not None:
        where.append("r.cooking_time_min IS NOT NULL AND r.cooking_time_min <= ?")
        params.append(int(max_time_min))

    if tags:
        for tag in tags:
            where.append("EXISTS (SELECT 1 FROM recipe_tags WHERE recipe_id = r.id AND tag = ?)")
            params.append(tag.lower().strip())

    if favorites_only:
        where.append("rf.recipe_id IS NOT NULL")

    if sources:
        cleaned = [s.strip() for s in sources if s and s.strip()]
        if cleaned:
            placeholders = ",".join("?" * len(cleaned))
            where.append(f"r.source IN ({placeholders})")
            params.extend(cleaned)

    with connect(db) as conn:
        if ingredients:
            groups = _resolve_ingredient_canonical_ids(conn, ingredients)
            if ingredient_mode == "and":
                # Each term must be satisfied; a term may match any of its ids.
                for ids in groups:
                    if not ids:
                        return []  # term resolved to nothing → unsatisfiable
                    where.append(_canonical_id_exists_clause(ids))
                    params.extend(ids)
            else:  # or — any id from any term satisfies
                all_ids = [cid for ids in groups for cid in ids]
                if not all_ids:
                    return []  # nothing resolved → no recipe can satisfy
                where.append(_canonical_id_exists_clause(all_ids))
                params.extend(all_ids)

        if pantry_only:
            where.append(
                "NOT EXISTS (SELECT 1 FROM recipe_ingredients ri "
                "WHERE ri.recipe_id = r.id AND ri.canonical_id IS NOT NULL "
                "AND ri.canonical_id NOT IN (SELECT canonical_id FROM pantry))"
            )
            # require at least one mapped ingredient so empty/all-unmapped recipes
            # don't qualify trivially
            where.append(
                "EXISTS (SELECT 1 FROM recipe_ingredients ri "
                "WHERE ri.recipe_id = r.id AND ri.canonical_id IS NOT NULL)"
            )

        sql = sql_base
        if where:
            sql += " AND " + " AND ".join(where)
        sql += f" {order} LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
    results = [_row_to_dict(r) for r in rows]
    for r in results:
        r["is_favorite"] = bool(r.get("is_favorite"))
    return results


def list_recipe_sources(*, db_path: Path | None = None) -> list[str]:
    """Distinct ``source`` values currently in the recipes table, sorted."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        rows = conn.execute("SELECT DISTINCT source FROM recipes ORDER BY source").fetchall()
    return [r["source"] for r in rows]


def get_recipe(recipe_id: int, *, db_path: Path | None = None) -> dict | None:
    """Fetch a recipe with its full ingredient list (canonical names joined) and tags."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (int(recipe_id),)).fetchone()
        if row is None:
            return None
        recipe = _row_to_dict(row)
        ings = conn.execute(
            "SELECT ri.id, ri.canonical_id, ri.original_text, ri.quantity, "
            "       ri.unit, ri.notes, ci.name AS canonical_name "
            "FROM recipe_ingredients ri "
            "LEFT JOIN canonical_ingredients ci ON ci.id = ri.canonical_id "
            "WHERE ri.recipe_id = ? ORDER BY ri.id",
            (recipe_id,),
        ).fetchall()
        tags = conn.execute(
            "SELECT tag FROM recipe_tags WHERE recipe_id = ? ORDER BY tag",
            (recipe_id,),
        ).fetchall()
        fav = conn.execute(
            "SELECT 1 FROM recipe_favorites WHERE recipe_id = ?", (recipe_id,)
        ).fetchone()
    recipe["ingredients"] = [_row_to_dict(i) for i in ings]
    recipe["tags"] = [t["tag"] for t in tags]
    recipe["is_favorite"] = fav is not None
    return recipe


def delete_recipe(recipe_id: int, *, db_path: Path | None = None) -> None:
    """Delete a recipe by id.

    Cascades to recipe_ingredients, recipe_tags, recipe_favorites, and
    meal_plan_items via ``ON DELETE CASCADE`` foreign keys. The single-row
    DELETE keeps the ``recipes_fts`` index in sync via the ``recipes_ad``
    trigger. Raises ``ValueError`` if the recipe doesn't exist.
    """
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute("DELETE FROM recipes WHERE id = ?", (int(recipe_id),))
        if cur.rowcount == 0:
            raise ValueError(f"recipe {recipe_id} not found")


def set_recipe_favorite(
    recipe_id: int,
    favorite: bool,
    *,
    db_path: Path | None = None,
) -> dict:
    """Mark or unmark a recipe as a favorite. Returns {recipe_id, is_favorite}.

    Raises ValueError if the recipe doesn't exist.
    """
    db = db_path or DB_PATH
    with connect(db) as conn:
        exists = conn.execute("SELECT 1 FROM recipes WHERE id = ?", (int(recipe_id),)).fetchone()
        if exists is None:
            raise ValueError(f"recipe {recipe_id} not found")
        if favorite:
            conn.execute(
                "INSERT INTO recipe_favorites (recipe_id) VALUES (?) "
                "ON CONFLICT(recipe_id) DO NOTHING",
                (int(recipe_id),),
            )
        else:
            conn.execute(
                "DELETE FROM recipe_favorites WHERE recipe_id = ?",
                (int(recipe_id),),
            )
    return {"recipe_id": int(recipe_id), "is_favorite": bool(favorite)}


# ---------- Pantry ----------


def list_pantry(*, db_path: Path | None = None) -> list[dict]:
    """List all pantry rows joined with canonical name and category."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT p.id, p.canonical_id, p.quantity, p.unit, p.added_at, "
            "       p.expires_at, p.note, ci.name AS canonical_name, ci.category "
            "FROM pantry p JOIN canonical_ingredients ci ON ci.id = p.canonical_id "
            "ORDER BY ci.name, p.id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def add_pantry_item(
    canonical_id: int,
    quantity: float,
    unit: str | None = None,
    expires_at: str | None = None,
    note: str | None = None,
    *,
    db_path: Path | None = None,
) -> dict:
    """Insert a pantry row. To adjust an existing row's quantity, remove and re-add."""
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO pantry (canonical_id, quantity, unit, expires_at, note) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id, canonical_id, quantity, unit, expires_at, note",
            (int(canonical_id), float(quantity), unit, expires_at, note),
        )
        row = cur.fetchone()
    return _row_to_dict(row)


def remove_pantry_item(item_id: int, *, db_path: Path | None = None) -> dict:
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute("DELETE FROM pantry WHERE id = ?", (int(item_id),))
        removed = cur.rowcount > 0
    return {"removed": removed, "id": int(item_id)}


def update_pantry_item(
    item_id: int,
    quantity: float,
    unit: str | None = None,
    *,
    db_path: Path | None = None,
) -> dict:
    """Update an existing pantry row's quantity and unit. Raises ValueError if missing or quantity < 0."""
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute(
            "UPDATE pantry SET quantity = ?, unit = ? WHERE id = ? "
            "RETURNING id, canonical_id, quantity, unit, expires_at, note",
            (float(quantity), unit, int(item_id)),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"pantry item {item_id} not found")
    return _row_to_dict(row)


# ---------- Canonical ingredient lookup ----------


def find_canonical_ingredient(
    query: str,
    limit: int = 10,
    *,
    db_path: Path | None = None,
) -> list[dict]:
    """Find canonical ingredients by partial name or alias match.

    Use this before ``add_pantry_item`` to translate an English ingredient
    name (e.g. "broccoli") to a canonical_id.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    db = db_path or DB_PATH
    pattern = f"%{q}%"
    prefix = f"{q}%"
    limit = _clamp_limit(limit)
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT id, name, category, default_unit, aliases, freshness_days "
            "FROM canonical_ingredients "
            "WHERE LOWER(name) LIKE ? OR LOWER(aliases) LIKE ? "
            "ORDER BY CASE WHEN LOWER(name) = ? THEN 0 "
            "              WHEN LOWER(name) LIKE ? THEN 1 "
            "              ELSE 2 END, name "
            "LIMIT ?",
            (pattern, pattern, q, prefix, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------- Meal plans ----------


def _validate_week_of(week_of: str) -> None:
    if not isinstance(week_of, str) or not _WEEK_OF_RE.match(week_of):
        raise ValueError("week_of must be an ISO date string YYYY-MM-DD")
    try:
        date.fromisoformat(week_of)
    except ValueError as e:
        raise ValueError("week_of must be an ISO date string YYYY-MM-DD") from e


def create_meal_plan(
    week_of: str,
    notes: str | None = None,
    *,
    db_path: Path | None = None,
) -> dict:
    """Create an empty draft meal plan for the given week.

    Idempotent on the (week_of, status='draft') partial unique index: if a
    draft already exists for that week, the existing row is returned and
    ``notes`` is ignored. Confirmed plans for the same week do not block
    creation of a new draft.
    """
    _validate_week_of(week_of)
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO meal_plans (week_of, notes) VALUES (?, ?) "
            "ON CONFLICT(week_of) WHERE status='draft' DO NOTHING "
            "RETURNING id, week_of, status, notes, created_at",
            (week_of, notes),
        )
        row = cur.fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, week_of, status, notes, created_at FROM meal_plans "
                "WHERE week_of = ? AND status = 'draft' ORDER BY id LIMIT 1",
                (week_of,),
            ).fetchone()
    return _row_to_dict(row)


def add_recipe_to_plan(
    plan_id: int,
    recipe_id: int,
    day: str | None = None,
    meal_slot: str | None = None,
    servings_planned: int = 1,
    *,
    db_path: Path | None = None,
) -> dict:
    if day is not None and day not in _DAY_VALUES:
        raise ValueError(f"day must be one of {sorted(_DAY_VALUES)} or None")
    if meal_slot is not None and meal_slot not in _MEAL_SLOT_VALUES:
        raise ValueError(f"meal_slot must be one of {sorted(_MEAL_SLOT_VALUES)} or None")
    if servings_planned < 1:
        raise ValueError("servings_planned must be >= 1")
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO meal_plan_items "
            "(plan_id, recipe_id, day, meal_slot, servings_planned) "
            "VALUES (?, ?, ?, ?, ?) "
            "RETURNING id, plan_id, recipe_id, day, meal_slot, servings_planned",
            (int(plan_id), int(recipe_id), day, meal_slot, int(servings_planned)),
        )
        row = cur.fetchone()
    return _row_to_dict(row)


def remove_meal_plan_item(item_id: int, *, db_path: Path | None = None) -> dict:
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute("DELETE FROM meal_plan_items WHERE id = ?", (int(item_id),))
        removed = cur.rowcount > 0
    return {"removed": removed, "id": int(item_id)}


def remove_meal_plan_item_from_plan(
    plan_id: int, item_id: int, *, db_path: Path | None = None
) -> dict:
    """Delete a meal_plan_items row only when its plan_id matches. Raises ValueError on mismatch."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        cur = conn.execute(
            "DELETE FROM meal_plan_items WHERE id = ? AND plan_id = ?",
            (int(item_id), int(plan_id)),
        )
        if cur.rowcount == 0:
            raise ValueError(f"item {item_id} not found in plan {plan_id}")
    return {"removed": True, "plan_id": int(plan_id), "item_id": int(item_id)}


def add_to_current_week_plan(
    recipe_id: int,
    week_of: str | None = None,
    *,
    db_path: Path | None = None,
) -> dict:
    """Find-or-create draft plan for ``week_of`` (defaults to current_sunday()) then insert item.

    ``week_of`` must be an ISO date string; if omitted the current week is used.
    Opens its own connection. Do not share connections across threads.
    """
    if week_of is not None:
        _validate_week_of(week_of)
        sunday = week_of
    else:
        sunday = current_sunday().isoformat()
    db = db_path or DB_PATH
    with connect(db) as conn:
        exists = conn.execute("SELECT 1 FROM recipes WHERE id = ?", (int(recipe_id),)).fetchone()
        if exists is None:
            raise ValueError(f"recipe {recipe_id} not found")
        conn.execute(
            "INSERT INTO meal_plans (week_of, status) VALUES (?, 'draft') "
            "ON CONFLICT(week_of) WHERE status='draft' DO NOTHING",
            (sunday,),
        )
        plan = conn.execute(
            "SELECT id FROM meal_plans WHERE week_of = ? AND status = 'draft' ORDER BY id LIMIT 1",
            (sunday,),
        ).fetchone()
        item = conn.execute(
            "INSERT INTO meal_plan_items "
            "(plan_id, recipe_id, day, meal_slot, servings_planned) "
            "VALUES (?, ?, NULL, NULL, 1) "
            "RETURNING id, plan_id, recipe_id, day, meal_slot, servings_planned",
            (plan["id"], int(recipe_id)),
        ).fetchone()
    return {"plan_id": plan["id"], "item": _row_to_dict(item)}


def set_meal_plan_favorite(
    plan_id: int,
    favorite: bool,
    *,
    db_path: Path | None = None,
) -> dict:
    """Mark or unmark a meal plan as a favorite. Returns {plan_id, is_favorite}."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        exists = conn.execute("SELECT 1 FROM meal_plans WHERE id = ?", (int(plan_id),)).fetchone()
        if exists is None:
            raise ValueError(f"meal plan {plan_id} not found")
        if favorite:
            conn.execute(
                "INSERT INTO meal_plan_favorites (plan_id) VALUES (?) "
                "ON CONFLICT(plan_id) DO NOTHING",
                (int(plan_id),),
            )
        else:
            conn.execute(
                "DELETE FROM meal_plan_favorites WHERE plan_id = ?",
                (int(plan_id),),
            )
    return {"plan_id": int(plan_id), "is_favorite": bool(favorite)}


def clone_meal_plan(plan_id: int, *, db_path: Path | None = None) -> dict:
    """Clone a meal plan into a new draft for a new week. Single transaction.

    Uses a week_of one week after the source plan to avoid conflicting with the
    partial unique index on (week_of) WHERE status='draft'.
    """
    db = db_path or DB_PATH
    with connect(db) as conn:
        source = conn.execute(
            "SELECT id, week_of, notes FROM meal_plans WHERE id = ?", (int(plan_id),)
        ).fetchone()
        if source is None:
            raise ValueError(f"meal plan {plan_id} not found")
        sunday = current_sunday().isoformat()
        existing_draft = conn.execute(
            "SELECT id FROM meal_plans WHERE week_of = ? AND status = 'draft'",
            (sunday,),
        ).fetchone()
        if existing_draft is not None:
            sunday = (current_sunday() + timedelta(days=7)).isoformat()
        new_notes = f"Cloned from #{plan_id}."
        if source["notes"]:
            new_notes += f" {source['notes']}"
        new_plan = conn.execute(
            "INSERT INTO meal_plans (week_of, status, notes) VALUES (?, 'draft', ?) "
            "RETURNING id, week_of, status, notes, created_at",
            (sunday, new_notes),
        ).fetchone()
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id, day, meal_slot, servings_planned) "
            "SELECT ?, recipe_id, day, meal_slot, servings_planned "
            "FROM meal_plan_items WHERE plan_id = ?",
            (new_plan["id"], int(plan_id)),
        )
        item_count = conn.execute(
            "SELECT COUNT(*) FROM meal_plan_items WHERE plan_id = ?",
            (new_plan["id"],),
        ).fetchone()[0]
    result = _row_to_dict(new_plan)
    result["item_count"] = item_count
    return result


def _pantry_coverage_for_plan(conn: sqlite3.Connection, plan_id: int) -> dict:
    """Shared coverage computation. Caller owns the connection.

    Does NOT validate plan existence — caller decides whether a missing plan
    yields {covered:0, total:0, percent:0, missing:[]} or raises.
    """
    plan_canonicals = conn.execute(
        "SELECT DISTINCT ri.canonical_id "
        "FROM meal_plan_items mpi "
        "JOIN recipe_ingredients ri ON ri.recipe_id = mpi.recipe_id "
        "WHERE mpi.plan_id = ? AND ri.canonical_id IS NOT NULL",
        (int(plan_id),),
    ).fetchall()
    canonical_ids = {r["canonical_id"] for r in plan_canonicals}
    if not canonical_ids:
        return {"plan_id": plan_id, "covered": 0, "total": 0, "percent": 0, "missing": []}
    pantry_ids = {
        r["canonical_id"]
        for r in conn.execute("SELECT DISTINCT canonical_id FROM pantry").fetchall()
    }
    covered_ids = canonical_ids & pantry_ids
    missing_ids = canonical_ids - pantry_ids
    missing = []
    if missing_ids:
        placeholders = ",".join("?" * len(missing_ids))
        rows = conn.execute(
            f"SELECT id, name FROM canonical_ingredients "  # noqa: S608
            f"WHERE id IN ({placeholders}) ORDER BY name",
            list(missing_ids),
        ).fetchall()
        missing = [{"canonical_id": r["id"], "name": r["name"]} for r in rows]
    total = len(canonical_ids)
    covered = len(covered_ids)
    return {
        "plan_id": plan_id,
        "covered": covered,
        "total": total,
        "percent": int(round(100 * covered / total)),
        "missing": missing,
    }


def compute_pantry_coverage(plan_id: int, *, db_path: Path | None = None) -> dict:
    """Compute pantry coverage for a plan.

    Returns {plan_id, covered, total, percent, missing}. Raises ValueError if
    the plan does not exist.
    """
    db = db_path or DB_PATH
    with connect(db) as conn:
        exists = conn.execute("SELECT 1 FROM meal_plans WHERE id = ?", (int(plan_id),)).fetchone()
        if exists is None:
            raise ValueError(f"meal plan {plan_id} not found")
        return _pantry_coverage_for_plan(conn, plan_id)


def _coverage_by_plan(*, db_path: Path | None = None) -> dict[int, dict]:
    """Compute coverage stats for all plans in one query. Returns {plan_id: {covered, total, percent}}."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        rows = conn.execute(
            """
            WITH plan_canon AS (
                SELECT DISTINCT mpi.plan_id, ri.canonical_id
                FROM meal_plan_items mpi
                JOIN recipe_ingredients ri ON ri.recipe_id = mpi.recipe_id
                WHERE ri.canonical_id IS NOT NULL
            )
            SELECT plan_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN canonical_id IN (SELECT canonical_id FROM pantry)
                       THEN 1 ELSE 0 END) AS covered
            FROM plan_canon GROUP BY plan_id
            """
        ).fetchall()
    result: dict[int, dict] = {}
    for r in rows:
        total = r["total"]
        covered = r["covered"]
        result[r["plan_id"]] = {
            "covered": covered,
            "total": total,
            "percent": int(round(100 * covered / total)) if total else 0,
        }
    return result


def list_meal_plans(*, db_path: Path | None = None) -> list[dict]:
    """All meal plans ordered by favorite status then newest week_of, with item counts and coverage."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT mp.id, mp.week_of, mp.status, mp.notes, mp.created_at, "
            "       COUNT(mpi.id) AS item_count, "
            "       (mpf.plan_id IS NOT NULL) AS is_favorite "
            "FROM meal_plans mp "
            "LEFT JOIN meal_plan_items mpi ON mpi.plan_id = mp.id "
            "LEFT JOIN meal_plan_favorites mpf ON mpf.plan_id = mp.id "
            "GROUP BY mp.id "
            "ORDER BY is_favorite DESC, mp.week_of DESC, mp.id DESC"
        ).fetchall()
    coverage_map = _coverage_by_plan(db_path=db)
    default_coverage = {"covered": 0, "total": 0, "percent": 0}
    results = []
    for r in rows:
        d = _row_to_dict(r)
        d["is_favorite"] = bool(d["is_favorite"])
        d["coverage"] = coverage_map.get(d["id"], default_coverage)
        results.append(d)
    return results


def get_meal_plan(plan_id: int, *, db_path: Path | None = None) -> dict | None:
    """Fetch a meal plan with all items, recipe names, favorite status, and coverage."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        row = conn.execute(
            "SELECT mp.id, mp.week_of, mp.status, mp.notes, mp.created_at, "
            "       (mpf.plan_id IS NOT NULL) AS is_favorite "
            "FROM meal_plans mp "
            "LEFT JOIN meal_plan_favorites mpf ON mpf.plan_id = mp.id "
            "WHERE mp.id = ?",
            (int(plan_id),),
        ).fetchone()
        if row is None:
            return None
        plan = _row_to_dict(row)
        plan["is_favorite"] = bool(plan["is_favorite"])
        items = conn.execute(
            "SELECT mpi.id, mpi.recipe_id, mpi.day, mpi.meal_slot, mpi.servings_planned, "
            "       r.name AS recipe_name, r.cooking_time_min, r.image_url "
            "FROM meal_plan_items mpi JOIN recipes r ON r.id = mpi.recipe_id "
            "WHERE mpi.plan_id = ? ORDER BY mpi.id",
            (plan_id,),
        ).fetchall()
        plan["items"] = [_row_to_dict(i) for i in items]
        plan["coverage"] = _pantry_coverage_for_plan(conn, plan_id)
    return plan


# ---------- Shopping list (qualitative v1) ----------


def compute_shopping_list(plan_id: int, *, db_path: Path | None = None) -> dict:
    """Aggregate canonical ingredients required by a plan, minus what the pantry covers.

    v1 is **qualitative**: no quantity comparison, because recipe ingredient
    quantities/units are not yet parsed (see BACKLOG.md). Output:

        {
            "plan_id": int,
            "needed":            [{canonical_id, name, category, in_recipes: [str]}],
            "covered_by_pantry": [{canonical_id, name, category, in_recipes: [str]}],
            "uncategorized":     [{recipe_id, recipe_name, original_text}]
        }
    """
    db = db_path or DB_PATH
    with connect(db) as conn:
        plan = conn.execute("SELECT id FROM meal_plans WHERE id = ?", (int(plan_id),)).fetchone()
        if plan is None:
            raise ValueError(f"meal plan {plan_id} not found")

        mapped = conn.execute(
            "SELECT ri.canonical_id, ci.name AS canonical_name, ci.category, "
            "       r.name AS recipe_name "
            "FROM meal_plan_items mpi "
            "JOIN recipes r ON r.id = mpi.recipe_id "
            "JOIN recipe_ingredients ri ON ri.recipe_id = r.id "
            "JOIN canonical_ingredients ci ON ci.id = ri.canonical_id "
            "WHERE mpi.plan_id = ?",
            (plan_id,),
        ).fetchall()

        unmapped = conn.execute(
            "SELECT ri.original_text, r.id AS recipe_id, r.name AS recipe_name "
            "FROM meal_plan_items mpi "
            "JOIN recipes r ON r.id = mpi.recipe_id "
            "JOIN recipe_ingredients ri ON ri.recipe_id = r.id "
            "WHERE mpi.plan_id = ? AND ri.canonical_id IS NULL "
            "ORDER BY r.id, ri.id",
            (plan_id,),
        ).fetchall()

        pantry_ids = {
            r["canonical_id"] for r in conn.execute("SELECT canonical_id FROM pantry").fetchall()
        }

    by_canonical: dict[int, dict] = {}
    for row in mapped:
        cid = row["canonical_id"]
        entry = by_canonical.setdefault(
            cid,
            {
                "canonical_id": cid,
                "name": row["canonical_name"],
                "category": row["category"],
                "in_recipes": [],
            },
        )
        if row["recipe_name"] not in entry["in_recipes"]:
            entry["in_recipes"].append(row["recipe_name"])

    needed = sorted(
        (v for k, v in by_canonical.items() if k not in pantry_ids),
        key=lambda x: x["name"],
    )
    covered = sorted(
        (v for k, v in by_canonical.items() if k in pantry_ids),
        key=lambda x: x["name"],
    )

    return {
        "plan_id": plan_id,
        "needed": needed,
        "covered_by_pantry": covered,
        "uncategorized": [_row_to_dict(r) for r in unmapped],
    }


# ---------- Ingredient mapping queue (web review UI; not MCP-exposed) ----------

_MAPPING_STATUSES = {"proposed", "approved", "rejected", "no_match"}


def list_mapping_queue(
    status: str | None = "proposed",
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    *,
    db_path: Path | None = None,
) -> dict:
    """Return queued mappings + per-status counts + distinct sources.

    ``status='proposed'`` is the default review queue. Pass ``None`` for all.
    Note: ``no_match`` rows are stored as ``status='proposed'`` with
    ``proposed_canonical_id=NULL`` (see ``upsert_mapping``); they surface in
    the same proposed list and are flagged in the row dict.
    """
    db = db_path or DB_PATH
    where: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        if status not in _MAPPING_STATUSES:
            raise ValueError(f"invalid status: {status}")
        where.append("q.status = ?")
        params.append(status)
    if source:
        where.append("q.source = ?")
        params.append(source)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with connect(db) as conn:
        rows = conn.execute(
            # noqa: S608 — where_sql is built from validated keys with ? placeholders
            f"""
            SELECT q.id, q.source, q.source_key, q.original_text,
                   q.proposed_canonical_id, q.confidence, q.status,
                   c.name AS canonical_name, c.category AS canonical_category
            FROM ingredient_mapping_queue q
            LEFT JOIN canonical_ingredients c ON c.id = q.proposed_canonical_id
            {where_sql}
            ORDER BY (q.proposed_canonical_id IS NULL), q.confidence DESC, q.id
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            (*params, _clamp_limit(limit), max(0, int(offset))),
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM ingredient_mapping_queue q {where_sql}",  # noqa: S608
            params,
        ).fetchone()[0]

        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM ingredient_mapping_queue GROUP BY status"
            ).fetchall()
        }
        sources = [
            r["source"]
            for r in conn.execute(
                "SELECT DISTINCT source FROM ingredient_mapping_queue ORDER BY source"
            ).fetchall()
        ]

    items = []
    for r in rows:
        d = _row_to_dict(r)
        d["is_no_match"] = d["proposed_canonical_id"] is None
        items.append(d)

    return {
        "items": items,
        "total": total,
        "counts": counts,
        "sources": sources,
    }


def get_mapping_queue_item(queue_id: int, *, db_path: Path | None = None) -> dict | None:
    """Fetch one queue row joined with the proposed canonical (if any)."""
    db = db_path or DB_PATH
    with connect(db) as conn:
        row = conn.execute(
            """
            SELECT q.id, q.source, q.source_key, q.original_text,
                   q.proposed_canonical_id, q.confidence, q.status,
                   c.name AS canonical_name, c.category AS canonical_category
            FROM ingredient_mapping_queue q
            LEFT JOIN canonical_ingredients c ON c.id = q.proposed_canonical_id
            WHERE q.id = ?
            """,
            (int(queue_id),),
        ).fetchone()
    if row is None:
        return None
    d = _row_to_dict(row)
    d["is_no_match"] = d["proposed_canonical_id"] is None
    return d
