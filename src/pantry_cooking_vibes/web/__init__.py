"""FastAPI read-only web UI (pantry is read-write)."""

from pantry_cooking_vibes.web.app import create_app

__all__ = ["create_app"]
