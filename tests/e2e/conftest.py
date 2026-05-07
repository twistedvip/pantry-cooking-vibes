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


def _upsert_canonical(conn, name: str, category: str) -> int:
    """Idempotently insert a canonical ingredient and return its id."""
    return conn.execute(
        "INSERT INTO canonical_ingredients (name, category) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET name=excluded.name RETURNING id",
        (name, category),
    ).fetchone()["id"]


@pytest.fixture(scope="session")
def e2e_db(tmp_path_factory) -> Path:
    """Seeded DB exercising every read/write surface in the web UI.

    'salt' is intentionally not used by any recipe or pantry row so the pantry
    add/remove e2e can search/add it without colliding with pre-existing state.
    """
    db = tmp_path_factory.mktemp("e2e") / "app.db"
    init_db(db_path=db)
    with connect(db) as conn:
        chicken_id = _upsert_canonical(conn, "chicken breast", "protein")
        olive_oil_id = _upsert_canonical(conn, "olive oil", "pantry")
        tofu_id = _upsert_canonical(conn, "tofu", "protein")
        _upsert_canonical(conn, "salt", "pantry")

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
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, ?, ?)",
            (r1, olive_oil_id, "2 tbsp olive oil"),
        )
        conn.execute("INSERT INTO recipe_tags VALUES (?, 'quick')", (r1,))
        r2 = conn.execute(
            "INSERT INTO recipes (source, source_id, name, cooking_time_min, "
            "                     servings, instructions_md, rating) "
            "VALUES ('manual', 'e2e-2', 'Vegan Tofu Bowl', 20, 2, "
            "        'Cube and bake tofu.', 4.0) RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, ?, ?)",
            (r2, tofu_id, "14 oz tofu"),
        )

        conn.execute(
            "INSERT INTO pantry (canonical_id, quantity, unit) VALUES (?, 1, 'bottle')",
            (olive_oil_id,),
        )

        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of, status, notes) "
            "VALUES ('2026-05-04', 'draft', 'e2e seed plan') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id, day, meal_slot, servings_planned) "
            "VALUES (?, ?, 'mon', 'dinner', 4)",
            (plan_id, r1),
        )
        conn.execute(
            "INSERT INTO meal_plan_items (plan_id, recipe_id, day, meal_slot, servings_planned) "
            "VALUES (?, ?, 'tue', 'lunch', 2)",
            (plan_id, r2),
        )

        conn.execute(
            "INSERT INTO ingredient_mapping_queue "
            "(source, source_key, original_text, proposed_canonical_id, confidence, status) "
            "VALUES ('e2e', 'lemon-zest', 'lemon zest', ?, 0.82, 'proposed')",
            (chicken_id,),
        )
        conn.execute(
            "INSERT INTO ingredient_mapping_queue "
            "(source, source_key, original_text, proposed_canonical_id, confidence, status) "
            "VALUES ('e2e', 'mystery-spice', 'mystery spice', ?, 0.55, 'proposed')",
            (chicken_id,),
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
