"""Fixtures for browser-driven end-to-end tests.

Boots a real ``uvicorn`` subprocess against a seeded temporary SQLite DB and
yields the base URL. Session-scoped so Playwright's browser fixture can reuse
it across tests.

Run with::

    pip install -e .[dev,e2e]
    playwright install chromium
    pytest -m e2e
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantry_cooking_vibes.db import connect, init_db


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(f"Server at {url} did not become ready: {last_err!r}")


@pytest.fixture(scope="session")
def e2e_db(tmp_path_factory) -> Path:
    """Seeded DB with a chicken-adjacent recipe so search 'chicken' returns hits."""
    db = tmp_path_factory.mktemp("e2e") / "app.db"
    init_db(db_path=db)
    with connect(db) as conn:
        chicken_id = conn.execute(
            "INSERT INTO canonical_ingredients (name, category) "
            "VALUES ('chicken breast', 'protein') "
            "ON CONFLICT(name) DO UPDATE SET name=excluded.name "
            "RETURNING id"
        ).fetchone()["id"]
        r1 = conn.execute(
            "INSERT INTO recipes (source, source_id, name, cooking_time_min, "
            "                     servings, instructions_md, rating, rating_count) "
            "VALUES ('manual', 'e2e-1', 'Lemon Chicken Skillet', 25, 4, "
            "        'Sear chicken, add lemon.', 4.7, 50) RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, ?, ?)",
            (r1, chicken_id, "1 lb chicken breast"),
        )
        conn.execute("INSERT INTO recipe_tags VALUES (?, 'quick')", (r1,))
        conn.execute(
            "INSERT INTO recipes (source, source_id, name, cooking_time_min, "
            "                     servings, instructions_md, rating) "
            "VALUES ('manual', 'e2e-2', 'Vegan Tofu Bowl', 20, 2, "
            "        'Cube and bake tofu.', 4.0)"
        )
    return db


@pytest.fixture(scope="session")
def live_server(e2e_db: Path) -> Iterator[str]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["PANTRY_COOKING_VIBES_DB"] = str(e2e_db)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "pantry_cooking_vibes.web.app_factory:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_http(url + "/", timeout=30)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
