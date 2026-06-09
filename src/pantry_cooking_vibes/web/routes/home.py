"""Home page: a decision-first overview.

Answers the three questions the owner actually opens the app with — what can I
cook right now, what should I use up, and where does this week's plan stand —
instead of a wall of counts. The counts survive as a quiet one-line strip.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from pantry_cooking_vibes.dates import current_sunday
from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.mcp_server import tools
from pantry_cooking_vibes.web.deps import get_db_path, render

# Reuse the pantry surface's own expiry bucketing and quantity formatting so the
# home's "use it soon" rows speak the exact same freshness vocabulary as the
# pantry page (one source of truth for the dot colours and the qty display).
from pantry_cooking_vibes.web.routes.pantry import _fmt_qty, _freshness

router = APIRouter()

# Small caps on purpose: the home answers a glance, the dedicated pages carry
# the long tail. Four cook-now recipes fill one grid row; six soon-rows is a
# scannable column without becoming the whole pantry.
_READY_LIMIT = 4
_SOON_LIMIT = 6


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

    today = date.today()
    pantry = tools.list_pantry(db_path=db_path)

    # Ready to cook: recipes whose every *mapped* ingredient is already in the
    # pantry. Needs a non-empty pantry to mean anything (pantry_only has nothing
    # to match against otherwise), so skip the query when the pantry is empty.
    ready: list[dict] = []
    if pantry:
        ready = tools.search_recipes(pantry_only=True, limit=_READY_LIMIT, db_path=db_path)
        if ready:
            coverage = tools.pantry_coverage_for_recipes([r["id"] for r in ready], db_path=db_path)
            for r in ready:
                cov = coverage.get(r["id"])
                if cov and cov["mapped"]:
                    r["coverage"] = cov

    # Use it soon: pantry rows expiring within the soon window or already past,
    # nearest expiry first, so the glance flags what to cook before it turns.
    expiring: list[dict] = []
    for p in pantry:
        freshness = _freshness(p.get("expires_at"), today)
        if freshness in ("soon", "expired"):
            p["freshness"] = freshness
            p["quantity_display"] = _fmt_qty(p.get("quantity"))
            expiring.append(p)
    expiring.sort(key=lambda p: p.get("expires_at") or "")
    expiring = expiring[:_SOON_LIMIT]

    # This week: the plan whose week_of is the current Sunday, if one exists.
    # list_meal_plans is already sorted; a linear scan is fine at personal scale.
    this_sunday = current_sunday().isoformat()
    week_plan = None
    for pl in tools.list_meal_plans(db_path=db_path):
        if pl["week_of"] == this_sunday:
            week_plan = tools.get_meal_plan(pl["id"], db_path=db_path)
            break

    return render(
        request,
        "home.html",
        {
            "recipe_count": recipe_count,
            "pantry_count": pantry_count,
            "plan_count": plan_count,
            "pantry_empty": not pantry,
            "ready": ready,
            "expiring": expiring,
            "week_plan": week_plan,
            "this_sunday": this_sunday,
        },
    )
