"""Module-level ASGI app for ``uvicorn``. Reads ``PANTRY_COOKING_VIBES_DB`` env var (optional)."""

from __future__ import annotations

import os
from pathlib import Path

from pantry_cooking_vibes.web.app import create_app

_env_db = os.environ.get("PANTRY_COOKING_VIBES_DB")
app = create_app(db_path=Path(_env_db) if _env_db else None)
