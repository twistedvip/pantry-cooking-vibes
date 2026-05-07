"""Meal-plan browsing, authoring, and management."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from pantry_cooking_vibes.dates import current_sunday, is_sunday
from pantry_cooking_vibes.mcp_server import tools
from pantry_cooking_vibes.web.deps import get_db_path, render, safe_redirect

router = APIRouter(prefix="/plans")


@router.get("")
def list_plans(request: Request, db_path: Path = Depends(get_db_path)) -> object:
    plans = tools.list_meal_plans(db_path=db_path)
    return render(request, "plans/list.html", {"plans": plans, "current_sunday": current_sunday()})


@router.post("")
def create_plan(
    week_of: str = Form(default=""),
    notes: str = Form(default=""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    """Create a new plan. Web form enforces Sunday constraint."""
    if not week_of:
        week_of = current_sunday().isoformat()
    if not is_sunday(week_of):
        raise HTTPException(status_code=422, detail="week_of must be a Sunday")
    plan = tools.create_meal_plan(week_of, notes=notes or None, db_path=db_path)
    return RedirectResponse(url=f"/plans/{plan['id']}", status_code=303)


@router.get("/{plan_id}")
def plan_detail(
    request: Request,
    plan_id: int,
    db_path: Path = Depends(get_db_path),
) -> object:
    plan = tools.get_meal_plan(plan_id, db_path=db_path)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Meal plan {plan_id} not found")
    return render(request, "plans/detail.html", {"plan": plan})


@router.post("/{plan_id}/favorite")
def toggle_plan_favorite(
    plan_id: int,
    favorite: str = Form("1"),
    redirect_to: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    want_fav = favorite == "1"
    try:
        tools.set_meal_plan_favorite(plan_id, want_fav, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    dest = safe_redirect(redirect_to, f"/plans/{plan_id}")
    return RedirectResponse(url=dest, status_code=303)


@router.post("/{plan_id}/clone")
def clone_plan(
    plan_id: int,
    redirect_to: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    try:
        new_plan = tools.clone_meal_plan(plan_id, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    dest = safe_redirect(redirect_to, f"/plans/{new_plan['id']}")
    return RedirectResponse(url=dest, status_code=303)


@router.post("/{plan_id}/items/{item_id}/delete")
def delete_plan_item(
    plan_id: int,
    item_id: int,
    redirect_to: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    try:
        tools.remove_meal_plan_item_from_plan(plan_id, item_id, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    dest = safe_redirect(redirect_to, f"/plans/{plan_id}")
    return RedirectResponse(url=dest, status_code=303)


@router.get("/{plan_id}/shopping")
def plan_shopping(
    request: Request,
    plan_id: int,
    db_path: Path = Depends(get_db_path),
) -> object:
    try:
        shopping = tools.compute_shopping_list(plan_id, db_path=db_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    plan = tools.get_meal_plan(plan_id, db_path=db_path)
    return render(
        request,
        "plans/shopping.html",
        {"plan": plan, "shopping": shopping},
    )


@router.get("/{plan_id}/print")
def plan_print(
    request: Request,
    plan_id: int,
    db_path: Path = Depends(get_db_path),
) -> object:
    plan = tools.get_meal_plan(plan_id, db_path=db_path)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Meal plan {plan_id} not found")
    shopping = tools.compute_shopping_list(plan_id, db_path=db_path)
    ingredients_deduped: dict[str, dict] = {}
    for item in shopping["needed"] + shopping["covered_by_pantry"]:
        key = str(item["canonical_id"])
        if key not in ingredients_deduped:
            ingredients_deduped[key] = item
    for item in shopping["uncategorized"]:
        key = (item.get("original_text") or "").strip().lower()
        if key and key not in ingredients_deduped:
            ingredients_deduped[key] = {
                "name": item["original_text"],
                "category": None,
                "canonical_id": None,
            }
    ingredients_sorted = sorted(
        ingredients_deduped.values(),
        key=lambda x: (x.get("category") or "", x.get("name") or ""),
    )
    return render(
        request,
        "plans/print.html",
        {"plan": plan, "ingredients": ingredients_sorted},
    )
