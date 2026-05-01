"""FastAPI dependencies. Tests override ``get_db_path`` via ``app.dependency_overrides``."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from pantry_cooking_vibes.db import DB_PATH

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_db_path() -> Path:
    return DB_PATH


def get_templates() -> Jinja2Templates:
    return _templates


def render(request: Request, template: str, context: dict) -> Response:
    """Render a template with ``request`` in context (required by Jinja2Templates)."""
    return _templates.TemplateResponse(request, template, context)
