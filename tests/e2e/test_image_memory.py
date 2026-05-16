"""Pytest wrapper around ``scripts/check_image_memory.py``.

Skipped unless ``DOCKER_MEM_CHECK=1`` so default ``pytest -m e2e`` runs on
machines without Docker stay green. CI invokes the script directly; this
test exists for local repro::

    DOCKER_MEM_CHECK=1 pytest -m e2e tests/e2e/test_image_memory.py -s
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_image_memory.py"


pytestmark = pytest.mark.e2e


@pytest.mark.skipif(
    os.environ.get("DOCKER_MEM_CHECK") != "1",
    reason="set DOCKER_MEM_CHECK=1 to run the docker memory regression check",
)
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not on PATH")
def test_docker_image_idle_memory_under_budget(tmp_path: Path) -> None:
    max_mb = os.environ.get("DOCKER_MEM_MAX_MB", "70")
    out_json = tmp_path / "mem.json"
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--max-mb",
        max_mb,
        "--no-build",
        "--json",
        str(out_json),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, f"memory check failed (rc={proc.returncode})"
    data = json.loads(out_json.read_text())
    assert data["passed"] is True
