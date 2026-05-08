"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from pantry_cooking_vibes.web.deps import STATIC_DIR, get_db_path
from pantry_cooking_vibes.web.routes import home, mappings, pantry, plans, recipes

# CSP for the read-only UI:
#   * default-src 'self' — block all cross-origin loads not explicitly allowed.
#   * img-src + data: — recipe thumbnails come from external https hosts; SVG
#     placeholders embedded in style.css use data: URIs.
#   * style-src 'unsafe-inline' + Google Fonts CSS — base.html links the fonts
#     stylesheet and several templates use inline `style=""` attributes.
#   * font-src — fonts.gstatic.com hosts the woff2 files Google Fonts pulls.
#   * script-src 'unsafe-inline' — `onsubmit="return confirm(...)"` attributes
#     are still in templates (delete-recipe, etc). Refactoring to external JS
#     would let us drop 'unsafe-inline' here; tracked in BACKLOG (security
#     hardening).
#   * frame-ancestors 'none' — clickjacking guard alongside X-Frame-Options.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' https: data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_same_origin(request: Request) -> bool:
    """Allow the request only if Origin/Referer match the Host header.

    Browsers permit cross-origin form POSTs (no preflight on
    application/x-www-form-urlencoded), so a malicious page running locally
    while the dev server is up could fire ``POST /recipes/N/delete``. Without
    a session/CSRF token, the cheapest defense is rejecting POSTs whose
    Origin (or Referer) doesn't match Host. Requests that omit both headers
    (curl, the test client, MCP clients) are allowed through; the threat
    model here is browser-driven CSRF, not authenticated tooling.
    """
    host = request.headers.get("host", "")
    if not host:
        return False
    origin = request.headers.get("origin")
    if origin:
        return urlsplit(origin).netloc == host
    referer = request.headers.get("referer")
    if referer:
        return urlsplit(referer).netloc == host
    # No browser-supplied origin info; let CLI/test-client/MCP through.
    return True


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

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.method in _UNSAFE_METHODS and not _is_same_origin(request):
            return Response("cross-origin request blocked", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(home.router)
    app.include_router(recipes.router)
    app.include_router(pantry.router)
    app.include_router(plans.router)
    app.include_router(mappings.router)
    return app
