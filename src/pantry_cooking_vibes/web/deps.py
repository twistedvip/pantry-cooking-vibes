"""FastAPI dependencies. Tests override ``get_db_path`` via ``app.dependency_overrides``."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

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


def safe_redirect(target: str | None, fallback: str) -> str:
    """Return ``target`` only if it's a same-origin path, else ``fallback``.

    A protocol-relative URL like ``//evil.example/x`` starts with ``/`` but
    redirects cross-origin in every browser. ``/\\foo`` is the same trick on
    a Windows-aware proxy. Reject both. Validated target is rebuilt via
    ``"/" + tail`` so CodeQL's ``StringConcatAsSanitizer`` recognizes the
    right-operand as sanitized for ``py/url-redirection``.
    """
    if not target or not target.startswith("/"):
        return fallback
    if target.startswith("//") or target.startswith("/\\"):
        return fallback
    return "/" + target[1:]


def url_quote(s: str) -> str:
    """URL-encode a string for use in a query parameter (no safe chars)."""
    return quote(s, safe="")
