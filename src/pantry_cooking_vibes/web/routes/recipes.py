"""Read-only recipe browsing."""

from __future__ import annotations

import json
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


# The browse default favors recipes the user can most readily cook (issue #53).
# Single source for the web-layer default: the query-param default, the
# normalize_sort fallback, and the template's active-filter check all read it.
WEB_DEFAULT_SORT = "availability"

# Dropdown labels per sort value. The guard below means a sort mode added to
# tools.SORT_VALUES cannot ship without a dropdown entry (and forces a look at
# _sort_label's toolbar copy in the same breath).
_SORT_CHOICES = {
    "availability": "On-hand ingredients",
    "rating": "Top rated",
    "relevance": "Best match",
}
if set(_SORT_CHOICES) != set(tools.SORT_VALUES):
    raise RuntimeError(
        f"_SORT_CHOICES keys {tuple(_SORT_CHOICES)} out of sync with "
        f"tools.SORT_VALUES {tools.SORT_VALUES}"
    )


def _sort_label(sort: str, has_query: bool) -> str:
    """Human description of the ordering actually applied (toolbar copy).

    Lives next to the route (not the template) so the wording stays in one
    place with the ``_SORT_CHOICES`` labels and tracks ``tools._order_clause``.
    """
    if sort == "rating":
        return "top-rated first"
    if sort == "relevance":
        return "best match" if has_query else "top-rated first"
    return "most ingredients on hand" + (" (best match first)" if has_query else "")


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
    sort: str = Query(
        WEB_DEFAULT_SORT, description="Sort order: availability | rating | relevance"
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
    # An unknown sort falls back to the web default rather than 422-ing.
    sort_val = tools.normalize_sort(sort, default=WEB_DEFAULT_SORT)

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
            sort=sort_val,
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

    # Attach coverage so each card can show "what can I cook now".
    # include_planned=True makes the badge count the same have-set definition
    # the availability sort ranks by (pantry ∪ upcoming-plan ingredients), so
    # ranking and badge agree up to the two queries running back-to-back.
    # All-zero coverage renders no badge in the template, so no gate is needed.
    if results:
        coverage = tools.pantry_coverage_for_recipes(
            [r["id"] for r in results], include_planned=True, db_path=db_path
        )
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
            "sort": sort_val,
            "default_sort": WEB_DEFAULT_SORT,
            "sort_choices": tuple(_SORT_CHOICES.items()),
            # strip() to match tools' has_query: a whitespace-only q takes the
            # no-query SQL path, so the label must not claim "best match first".
            "sort_label": _sort_label(sort_val, bool(q.strip())),
            "total": total,
            "page": page_val,
            "total_pages": total_pages,
            "page_window": _page_window(page_val, total_pages),
        },
    )


# ---------------------------------------------------------------------------
# Legacy single-URL import. Superseded by the import inbox (/imports), which
# funnels every import — a single pasted URL included — through staging so it
# gets the same dup/review triage. Kept as a redirect so old links and bookmarks
# still land somewhere useful. Registered above /{recipe_id} so the literal
# "import" segment is never swallowed by the int path param.
# ---------------------------------------------------------------------------


@router.get("/import")
def import_recipe_redirect() -> RedirectResponse:
    return RedirectResponse(url="/imports/new", status_code=307)


# Display order, label, and unit for the compact macro dict stored in
# recipes.nutrition_json ({calories, protein_g, fat_g, carbs_g, fiber_g,
# sodium_mg}). Order is the one nutrition labels conventionally read in.
# NOTE: calories leads the panel as a unitless figure (its "Calories" label
# already names the unit), so the "kcal" below is intentionally not rendered;
# it is kept for completeness and in case the lead ever shows a unit again.
_NUTRITION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("calories", "Calories", "kcal"),
    ("protein_g", "Protein", "g"),
    ("carbs_g", "Carbs", "g"),
    ("fat_g", "Fat", "g"),
    ("fiber_g", "Fiber", "g"),
    ("sodium_mg", "Sodium", "mg"),
)

# The macro key the detail panel renders as the lead, separate from the ledger.
_NUTRITION_LEAD_KEY = "calories"


def _coerce_nutrition_value(value: object) -> float | None:
    """Coerce a stored macro value (number, numeric string, or blank) to float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _nutrition_rows(nutrition_json: object) -> list[dict[str, str]]:
    """Parse ``recipes.nutrition_json`` into ordered, display-ready macro rows.

    The column is stored as a JSON string but tolerated as an already-parsed
    dict here (``get_recipe`` returns the raw column verbatim). Returns one row
    ``{key, label, value, unit}`` per macro that carries a usable number, in the
    canonical label order. A missing column, malformed JSON, a non-object
    payload, or an all-empty dict yields ``[]`` — the template renders no panel
    in that case. ``value`` is formatted without a trailing ``.0`` (``12.0`` →
    ``12``) so whole numbers read cleanly.
    """
    if not nutrition_json:
        return []
    if isinstance(nutrition_json, str):
        try:
            data = json.loads(nutrition_json)
        except (ValueError, TypeError):
            return []
    else:
        data = nutrition_json
    if not isinstance(data, dict):
        return []
    macros: dict[str, object] = {str(k): v for k, v in data.items()}
    rows: list[dict[str, str]] = []
    for key, label, unit in _NUTRITION_FIELDS:
        value = _coerce_nutrition_value(macros.get(key))
        if value is None:
            continue
        rows.append({"key": key, "label": label, "value": f"{value:g}", "unit": unit})
    return rows


def _nutrition_panel(nutrition_json: object) -> dict[str, object] | None:
    """Split parsed macro rows into the calories lead and the macro ledger.

    Returns ``{"calories": <row|None>, "macros": [rows]}`` when any macro is
    present, else ``None`` (the template renders no panel). Owning the split
    here, keyed on the stable field key rather than a display label, keeps the
    template from reconstructing the lead/ledger structure by matching label
    text: relabelling a macro can't silently move it between lead and ledger.
    """
    rows = _nutrition_rows(nutrition_json)
    if not rows:
        return None
    calories = next((r for r in rows if r["key"] == _NUTRITION_LEAD_KEY), None)
    macros = [r for r in rows if r["key"] != _NUTRITION_LEAD_KEY]
    return {"calories": calories, "macros": macros}


@router.get("/{recipe_id}")
def recipe_detail(
    request: Request,
    recipe_id: int,
    saved: str = Query("", description="Set to 1 to flash a save confirmation"),
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
            "nutrition": _nutrition_panel(recipe.get("nutrition_json")),
            "pantry_canonical_ids": pantry_canonical_ids,
            "current_week": current_week,
            "next_week": next_week,
            "suggest_next_week": suggest_next_week,
            "saved": saved == "1",
        },
    )


def _edit_form_values(recipe: dict) -> dict:
    """Prefill values for the edit form, shaped as the form fields submit them."""
    return {
        "name": recipe["name"],
        "cooking_time_min": (
            "" if recipe["cooking_time_min"] is None else str(recipe["cooking_time_min"])
        ),
        "servings": "" if recipe["servings"] is None else str(recipe["servings"]),
        "image_url": recipe["image_url"] or "",
        "tags": ", ".join(recipe["tags"]),
        "ingredients": "\n".join(
            (i["original_text"] or i["canonical_name"] or "") for i in recipe["ingredients"]
        ),
        "instructions": recipe["instructions_md"] or "",
    }


@router.get("/{recipe_id}/edit")
def edit_recipe_form(
    request: Request,
    recipe_id: int,
    db_path: Path = Depends(get_db_path),
) -> object:
    recipe = tools.get_recipe(recipe_id, db_path=db_path)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Recipe {recipe_id} not found")
    return render(
        request,
        "recipes/edit.html",
        {"recipe": recipe, "form": _edit_form_values(recipe), "error": None},
    )


@router.post("/{recipe_id}/edit")
def edit_recipe_submit(
    request: Request,
    recipe_id: int,
    name: str = Form(""),
    cooking_time_min: str = Form(""),
    servings: str = Form(""),
    image_url: str = Form(""),
    tags: str = Form(""),
    ingredients: str = Form(""),
    instructions: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> object:
    """Apply a recipe edit; on validation failure re-render the form unchanged.

    All writes happen inside ``tools.update_recipe``'s single transaction, so
    a rejected edit leaves the stored recipe exactly as it was.
    """
    recipe = tools.get_recipe(recipe_id, db_path=db_path)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Recipe {recipe_id} not found")

    error: str | None = None
    time_val: int | None = None
    servings_val: int | None = None
    try:
        time_val = _parse_optional_int(cooking_time_min, "cooking time", min_value=0)
        servings_val = _parse_optional_int(servings, "servings", min_value=1)
    except HTTPException as e:
        error = str(e.detail)

    if error is None:
        try:
            tools.update_recipe(
                recipe_id,
                name=name,
                cooking_time_min=time_val,
                servings=servings_val,
                image_url=image_url or None,
                instructions=instructions.splitlines(),
                tags=tags.split(","),
                ingredients=ingredients.splitlines(),
                db_path=db_path,
            )
        except ValueError as e:
            error = str(e)
        else:
            # int() cast makes the int-ness explicit and breaks CodeQL's
            # url-redirection dataflow (recipe_id is already an int path param).
            return RedirectResponse(url=f"/recipes/{int(recipe_id)}?saved=1", status_code=303)

    # Echo the submitted values back (Jinja autoescape neutralizes any markup)
    # so the user can fix the one bad field instead of retyping everything.
    response = render(
        request,
        "recipes/edit.html",
        {
            "recipe": recipe,
            "form": {
                "name": name,
                "cooking_time_min": cooking_time_min,
                "servings": servings,
                "image_url": image_url,
                "tags": tags,
                "ingredients": ingredients,
                "instructions": instructions,
            },
            "error": error,
        },
    )
    response.status_code = 422
    return response


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
