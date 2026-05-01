"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pantry_cooking_vibes.web.deps import STATIC_DIR, get_db_path
from pantry_cooking_vibes.web.routes import home, mappings, pantry, plans, recipes


def create_app(db_path: Path | None = None) -> FastAPI:
    """Build a FastAPI app. Pass ``db_path`` to pin the database (used by tests)."""
    app = FastAPI(
        title="Meal Planner",
        description="Local, read-only browse UI for recipes and meal plans. Pantry is editable.",
        version="0.1.0",
    )

    if db_path is not None:
        resolved = Path(db_path)
        app.dependency_overrides[get_db_path] = lambda: resolved

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(home.router)
    app.include_router(recipes.router)
    app.include_router(pantry.router)
    app.include_router(plans.router)
    app.include_router(mappings.router)
    return app
