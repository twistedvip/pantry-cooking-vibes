"""Read-only recipe browsing."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

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
    # search_recipes sanitizes the FTS5 query, but DB-level errors at the
    # boundary (corrupt index, locked file, bad migration) shouldn't 500 the
    # whole page — log them, render an empty result so the UI stays usable.
    try:
        results = tools.search_recipes(
            query=q,
            max_time_min=max_time_val,
            tags=tag_list or None,
            limit=limit_val,
            favorites_only=favorites_only,
            sources=selected_sources or None,
            ingredients=ingredient_list or None,
            ingredient_mode=mode,
            pantry_only=pantry_only_val,
            db_path=db_path,
        )
    except sqlite3.OperationalError:
        # Strip CR/LF before logging so a crafted ?q= / ?tags= can't forge log
        # lines (CWE-117). The "\r\n" and "\n" replace calls are also what
        # CodeQL recognizes as a log-injection sanitizer.
        safe_q = q.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        safe_tags = [t.replace("\r\n", " ").replace("\n", " ").replace("\r", " ") for t in tag_list]
        log.exception("search_recipes failed: q=%r tags=%r", safe_q, safe_tags)
        results = []
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
    return render(
        request,
        "recipes/detail.html",
        {"recipe": recipe, "pantry_canonical_ids": pantry_canonical_ids},
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
    redirect_to: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    """Add a recipe to the current week's draft meal plan."""
    try:
        result = tools.add_to_current_week_plan(recipe_id, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    dest = safe_redirect(redirect_to, "/plans/" + str(result["plan_id"]))
    return RedirectResponse(url=dest, status_code=303)
