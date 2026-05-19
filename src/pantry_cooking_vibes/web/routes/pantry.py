"""Pantry: the only read-write surface in the web UI."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from pantry_cooking_vibes.mcp_server import tools
from pantry_cooking_vibes.web.deps import get_db_path, render, url_quote as _q

router = APIRouter(prefix="/pantry")

# Unit options scoped per canonical category so the dropdown shows only
# measurements that make sense for the ingredient (e.g. proteins by weight,
# spices by spoon). Free text retired in favor of <select> to stop
# typo-driven divergence (e.g. "tbs" vs "tbsp"). Categories mirror
# canonical_seed.csv. Order within each tuple = display order.
UNIT_OPTIONS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "protein": ("oz", "lb", "g", "kg", "slice", "link", "whole", "count"),
    "vegetable": (
        "whole",
        "cup",
        "oz",
        "lb",
        "g",
        "head",
        "bulb",
        "stalk",
        "spear",
        "ear",
        "clove",
        "leaf",
    ),
    "fruit": ("whole", "cup", "oz", "lb", "g"),
    "grain": ("cup", "oz", "lb", "g", "slice", "whole"),
    "dairy": ("cup", "oz", "lb", "g", "tbsp", "tsp", "slice", "ml", "l"),
    "legume": ("cup", "oz", "lb", "g", "tbsp"),
    "nut": ("oz", "lb", "g", "cup", "tbsp"),
    "seed": ("tbsp", "tsp", "oz", "g", "cup"),
    "fat": ("tbsp", "tsp", "cup", "ml", "l", "fl oz"),
    "herb": ("sprig", "leaf", "stalk", "cup", "tbsp", "tsp"),
    "spice": ("tsp", "tbsp", "pinch", "whole"),
    "condiment": ("tbsp", "tsp", "cup", "ml", "l", "fl oz"),
    "baking": ("cup", "tbsp", "tsp", "oz", "lb", "g", "whole"),
    "beverage": ("cup", "fl oz", "ml", "l", "pint", "quart", "gallon"),
}
# Catch-all for canonicals with NULL/unknown category. Kept short.
DEFAULT_UNIT_OPTIONS: tuple[str, ...] = (
    "count",
    "cup",
    "g",
    "kg",
    "lb",
    "oz",
    "tbsp",
    "tsp",
    "whole",
)


def _units_for(category: str | None) -> tuple[str, ...]:
    return UNIT_OPTIONS_BY_CATEGORY.get((category or "").lower(), DEFAULT_UNIT_OPTIONS)


@router.get("")
def pantry_page(
    request: Request,
    search: str = "",
    error: str = "",
    added: str = "",
    removed: str = "",
    updated: str = "",
    db_path: Path = Depends(get_db_path),
) -> object:
    items = tools.list_pantry(db_path=db_path)
    for p in items:
        p["unit_options"] = _units_for(p.get("category"))
    suggestions = (
        tools.find_canonical_ingredient(search, limit=20, db_path=db_path) if search.strip() else []
    )
    today = date.today()
    for s in suggestions:
        fd = s.get("freshness_days")
        s["suggested_expires_at"] = (today + timedelta(days=fd)).isoformat() if fd else ""
        s["unit_options"] = _units_for(s.get("category"))
    return render(
        request,
        "pantry/list.html",
        {
            "items": items,
            "search": search,
            "suggestions": suggestions,
            "error": error,
            "added": added,
            "removed": removed,
            "updated": updated,
        },
    )


@router.post("/add")
def pantry_add(
    canonical_id: int = Form(...),
    quantity: float = Form(...),
    unit: str = Form(""),
    expires_at: str = Form(""),
    note: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    unit_val: str | None = unit.strip() or None
    expires_val: str | None = expires_at.strip() or None
    note_val: str | None = note.strip() or None
    try:
        row = tools.add_pantry_item(
            canonical_id=canonical_id,
            quantity=quantity,
            unit=unit_val,
            expires_at=expires_val,
            note=note_val,
            db_path=db_path,
        )
    except ValueError as e:
        return RedirectResponse(url="/pantry?error=" + _q(str(e)), status_code=303)
    return RedirectResponse(
        url="/pantry?added=item%20" + str(row["id"]),
        status_code=303,
    )


@router.post("/{item_id}/update")
def pantry_update(
    item_id: int,
    quantity: float = Form(...),
    unit: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    unit_val: str | None = unit.strip() or None
    try:
        tools.update_pantry_item(
            item_id=item_id,
            quantity=quantity,
            unit=unit_val,
            db_path=db_path,
        )
    except ValueError as e:
        return RedirectResponse(url="/pantry?error=" + _q(str(e)), status_code=303)
    return RedirectResponse(
        url="/pantry?updated=item%20" + str(item_id),
        status_code=303,
    )


@router.post("/{item_id}/delete")
def pantry_delete(
    item_id: int,
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    result = tools.remove_pantry_item(item_id, db_path=db_path)
    if result["removed"]:
        return RedirectResponse(url="/pantry?removed=item%20" + str(item_id), status_code=303)
    return RedirectResponse(
        url="/pantry?error=item%20not%20found%3A%20" + str(item_id),
        status_code=303,
    )
