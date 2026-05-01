"""Read-only meal-plan browsing + shopping list."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from pantry_cooking_vibes.mcp_server import tools
from pantry_cooking_vibes.web.deps import get_db_path, render

router = APIRouter(prefix="/plans")


@router.get("")
def list_plans(request: Request, db_path: Path = Depends(get_db_path)) -> object:
    plans = tools.list_meal_plans(db_path=db_path)
    return render(request, "plans/list.html", {"plans": plans})


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
