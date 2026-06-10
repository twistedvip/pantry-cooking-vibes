"""Read-only recipe browsing."""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from pantry_cooking_vibes.dates import current_sunday, is_sunday, is_week_halfway_over, next_sunday
from pantry_cooking_vibes.mcp_server import tools
from pantry_cooking_vibes.web.deps import get_db_path, render, safe_redirect

log = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes")


def _parse_optional_int(raw: str, field: str, *, min_value: int = 0) -> int | None:
    """Coerce form-submitted strings to Optional[int].

    HTML forms submit blank number inputs as ``""``; FastAPI's native
    ``Optional[int] = Query(None)`` rejects that with a 422 "expects integer"
    error. This parses blanks to ``None`` and surfaces a 400 for genuinely
    non-numeric input.
    """
    if raw == "" or raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be an integer (got {raw!r})",
        ) from e
    if value < min_value:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be >= {min_value} (got {value})",
        )
    return value


_LIMIT_CHOICES = (50, 100, 250)
# The page/offset math trusts tools to honor these limits verbatim; a choice
# above the tools-side cap would silently make deep pages unreachable.
if max(_LIMIT_CHOICES) > tools.MAX_RESULT_LIMIT:
    raise RuntimeError(
        f"_LIMIT_CHOICES {_LIMIT_CHOICES} exceeds tools.MAX_RESULT_LIMIT "
        f"({tools.MAX_RESULT_LIMIT}); deep pages would be unreachable"
    )


def _page_window(current: int, total_pages: int) -> list[int | None]:
    """Page numbers worth rendering: 1 and last always, current ±2 between.

    ``None`` marks a gap the template renders as an ellipsis, e.g.
    ``[1, None, 4, 5, 6, 7, 8, None, 25]`` for page 6 of 25. An ellipsis is
    never noise here: short runs (≤7 pages) render in full, and a gap that
    would hide exactly one page emits that page instead.
    """
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    pages: list[int | None] = []
    previous = 0
    for p in range(1, total_pages + 1):
        if p == 1 or p == total_pages or abs(p - current) <= 2:
            if previous and p - previous == 2:
                pages.append(p - 1)
            elif previous and p - previous > 1:
                pages.append(None)
            pages.append(p)
            previous = p
    return pages


@router.get("")
def list_recipes(
    request: Request,
    q: str = Query("", description="Full-text search query"),
    max_time: str = Query("", description="Max cooking time in minutes (blank = no cap)"),
    tags: str = Query("", description="Comma-separated tag list"),
    limit: str = Query("50", description="Result cap (one of 50/100/250)"),
    fav: str = Query("", description="Set to 1 to show favorites only"),
    sources: list[str] = Query(
        default_factory=list, description="Filter by source(s); empty = no restriction"
    ),
    ingredients: str = Query("", description="Comma-separated canonical ingredient names"),
    ingredient_mode: str = Query("and", description="'and' (all) or 'or' (any)"),
    pantry_only: str = Query(
        "", description="Set to 1 to show only recipes whose mapped ingredients are all in pantry"
    ),
    # Unlike the filter fields, `page` never arrives as a blank form value —
    # only pager links set it — so native int parsing (422 on garbage) is fine.
    page: int = Query(1, ge=1, description="1-based result page"),
    db_path: Path = Depends(get_db_path),
) -> object:
    max_time_val = _parse_optional_int(max_time, "max_time", min_value=0)
    requested_limit = _parse_optional_int(limit, "limit", min_value=1) or 50
    limit_val = requested_limit if requested_limit in _LIMIT_CHOICES else 50
    favorites_only = fav == "1"
    pantry_only_val = pantry_only == "1"
    mode = ingredient_mode if ingredient_mode in ("and", "or") else "and"

    available_sources = tools.list_recipe_sources(db_path=db_path)
    selected_sources = [s for s in sources if s in available_sources]

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    ingredient_list = [i.strip() for i in ingredients.split(",") if i.strip()]
    # search_recipes_page sanitizes the FTS5 query, but DB-level errors at the
    # boundary (corrupt index, locked file, bad migration) shouldn't 500 the
    # whole page — log them, render an empty result so the UI stays usable.
    page_data: tools.RecipePage
    try:
        page_data = tools.search_recipes_page(
            query=q,
            max_time_min=max_time_val,
            tags=tag_list or None,
            limit=limit_val,
            favorites_only=favorites_only,
            sources=selected_sources or None,
            ingredients=ingredient_list or None,
            ingredient_mode=mode,
            pantry_only=pantry_only_val,
            offset=(page - 1) * limit_val,
            db_path=db_path,
        )
    except sqlite3.OperationalError:
        # Strip CR/LF before logging so a crafted ?q= / ?tags= can't forge log
        # lines (CWE-117). The "\r\n" and "\n" replace calls are also what
        # CodeQL recognizes as a log-injection sanitizer.
        safe_q = q.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        safe_tags = [t.replace("\r\n", " ").replace("\n", " ").replace("\r", " ") for t in tag_list]
        log.exception("search_recipes_page failed: q=%r tags=%r", safe_q, safe_tags)
        page_data = {"items": [], "total": 0, "offset": 0}

    results = page_data["items"]
    total = page_data["total"]
    # Derived once for both the happy and the error path. The effective page
    # comes from the offset tools actually used (it clamps past-the-end
    # offsets to the final page, and clamped offsets are limit-aligned).
    page_val = page_data["offset"] // limit_val + 1
    total_pages = max(1, math.ceil(total / limit_val))

    # Attach pantry coverage so each card can show "what can I cook now". Only
    # meaningful when the pantry has something in it; skip the work (and the
    # noise of all-zero badges) for an empty pantry.
    if results and tools.list_pantry(db_path=db_path):
        coverage = tools.pantry_coverage_for_recipes([r["id"] for r in results], db_path=db_path)
        for r in results:
            cov = coverage.get(r["id"])
            if cov and cov["mapped"]:
                r["coverage"] = cov

    return render(
        request,
        "recipes/list.html",
        {
            "recipes": results,
            "q": q,
            "max_time": max_time_val,
            "tags": ",".join(tag_list),
            "limit": limit_val,
            "limit_choices": _LIMIT_CHOICES,
            "favorites_only": favorites_only,
            "available_sources": available_sources,
            "selected_sources": selected_sources,
            "ingredients": ",".join(ingredient_list),
            "ingredient_mode": mode,
            "pantry_only": pantry_only_val,
            "total": total,
            "page": page_val,
            "total_pages": total_pages,
            "page_window": _page_window(page_val, total_pages),
        },
    )


@router.get("/{recipe_id}")
def recipe_detail(
    request: Request,
    recipe_id: int,
    db_path: Path = Depends(get_db_path),
) -> object:
    recipe = tools.get_recipe(recipe_id, db_path=db_path)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Recipe {recipe_id} not found")
    pantry_canonical_ids = {p["canonical_id"] for p in tools.list_pantry(db_path=db_path)}
    current_week = current_sunday().isoformat()
    next_week = next_sunday().isoformat()
    suggest_next_week = is_week_halfway_over()
    return render(
        request,
        "recipes/detail.html",
        {
            "recipe": recipe,
            "pantry_canonical_ids": pantry_canonical_ids,
            "current_week": current_week,
            "next_week": next_week,
            "suggest_next_week": suggest_next_week,
        },
    )


@router.post("/{recipe_id}/delete")
def delete_recipe(
    recipe_id: int,
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    """Delete a recipe and redirect to the recipes list."""
    try:
        tools.delete_recipe(recipe_id, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=f"Recipe {recipe_id} not found") from e
    return RedirectResponse(url="/recipes", status_code=303)


@router.post("/{recipe_id}/favorite")
def toggle_favorite(
    recipe_id: int,
    favorite: str = Form("1"),
    redirect_to: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    """Mark/unmark a recipe as favorite. ``favorite=1`` favs, anything else unfavs."""
    want_fav = favorite == "1"
    try:
        tools.set_recipe_favorite(recipe_id, want_fav, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=f"Recipe {recipe_id} not found") from e
    dest = safe_redirect(redirect_to, "/recipes/" + str(recipe_id))
    return RedirectResponse(url=dest, status_code=303)


@router.post("/{recipe_id}/add-to-current-week")
def add_to_current_week(
    recipe_id: int,
    week_of: str = Form(""),
    redirect_to: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    """Add a recipe to a draft meal plan for the chosen week."""
    if not week_of:
        week_of = current_sunday().isoformat()
    if not is_sunday(week_of):
        raise HTTPException(status_code=422, detail="week_of must be a Sunday")
    try:
        result = tools.add_to_current_week_plan(recipe_id, week_of=week_of, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    dest = safe_redirect(redirect_to, "/plans/" + str(result["plan_id"]))
    return RedirectResponse(url=dest, status_code=303)
