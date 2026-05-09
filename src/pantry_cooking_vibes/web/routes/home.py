"""Home page: one-glance overview of the meal planner."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.web.deps import get_db_path, render

router = APIRouter()


@router.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
def healthz() -> str:
    """Liveness probe for container healthchecks. No DB, no template."""
    return "ok"


@router.get("/")
def home(request: Request, db_path: Path = Depends(get_db_path)) -> object:
    with connect(db_path) as conn:
        recipe_count = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        pantry_count = conn.execute("SELECT COUNT(*) FROM pantry").fetchone()[0]
        plan_count = conn.execute("SELECT COUNT(*) FROM meal_plans").fetchone()[0]
        canonical_count = conn.execute("SELECT COUNT(*) FROM canonical_ingredients").fetchone()[0]
    return render(
        request,
        "home.html",
        {
            "recipe_count": recipe_count,
            "pantry_count": pantry_count,
            "plan_count": plan_count,
            "canonical_count": canonical_count,
        },
    )
