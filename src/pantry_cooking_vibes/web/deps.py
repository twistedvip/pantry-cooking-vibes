"""FastAPI dependencies. Tests override ``get_db_path`` via ``app.dependency_overrides``."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from pantry_cooking_vibes.db import DB_PATH

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def humandate(value: object) -> str:
    """ISO date/timestamp -> reading copy, e.g. "Jun 7" (or "Jun 7, 2025").

    Drops the year only when it matches the current year, so a date is never
    ambiguous across years. Falls back to the raw string if it can't parse.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        d = date.fromisoformat(text[:10])
    except ValueError:
        return text
    label = f"{d:%b} {d.day}"
    if d.year != date.today().year:
        label += f", {d.year}"
    return label


def humanstamp(value: object) -> str:
    """ISO timestamp -> "today" / "yesterday" / "Jun 8"; never shows the clock."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        d = date.fromisoformat(text[:10])
    except ValueError:
        return text
    delta = (date.today() - d).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return humandate(text)


def weeklabel(week_of: object, current_sunday: object = None) -> str:
    """A plan's week as reading copy, relative to the current week when possible.

    "This week" / "Next week" / "Last week" for the three adjacent Sundays,
    otherwise "Week of Jun 7". ``current_sunday`` is the ISO date of the user's
    current Sunday (passed from the route); without it, always absolute.
    """
    text = str(week_of or "").strip()
    try:
        d = date.fromisoformat(text[:10])
    except ValueError:
        return text
    anchor_text = str(current_sunday or "").strip()
    try:
        anchor = date.fromisoformat(anchor_text[:10]) if anchor_text else None
    except ValueError:
        anchor = None
    if anchor is not None:
        offset = (d - anchor).days
        if offset == 0:
            return "This week"
        if offset == 7:
            return "Next week"
        if offset == -7:
            return "Last week"
    return f"Week of {humandate(text)}"


_templates.env.filters["humandate"] = humandate
_templates.env.filters["humanstamp"] = humanstamp
_templates.env.filters["weeklabel"] = weeklabel


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
