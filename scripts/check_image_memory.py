"""Memory-budget regression check for the Docker image.

Builds (or reuses) the image, runs the container, warms the web UI, samples
memory N times via ``docker stats``, and fails if the maximum observed usage
exceeds the configured budget.

Why this lives outside pytest: the check is slow (image build, container
boot, warmup, N samples) and environment-sensitive (Docker required). The
companion pytest in ``tests/e2e/test_image_memory.py`` shells out to this
script for local repro; CI invokes the script directly.

Usage::

    python scripts/check_image_memory.py                    # build + check
    python scripts/check_image_memory.py --image TAG        # skip build
    python scripts/check_image_memory.py --max-mb 80        # raise budget
    python scripts/check_image_memory.py --json out.json    # emit metrics

Exit codes: 0 ok, 1 over budget, 2 setup failure (build/run/probe).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "pantry-cooking-vibes:mem-check"
DEFAULT_MAX_MB = 70.0
WARN_FRACTION = 0.85  # warn when max sample crosses 85% of budget
WARMUP_PATHS = ("/healthz", "/", "/recipes", "/pantry", "/plans")
WARMUP_LOOPS = 3
SAMPLE_COUNT = 10
SAMPLE_INTERVAL_S = 3.0
BOOT_TIMEOUT_S = 30.0


@dataclass
class Sample:
    t: float
    mb: float


@dataclass
class Result:
    image: str
    samples: list[Sample]
    max_mb: float
    mean_mb: float
    budget_mb: float
    passed: bool


def _run(
    cmd: list[str], *, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def _image_exists(tag: str) -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def build_image(tag: str) -> None:
    print(f"[build] docker build -t {tag} .", flush=True)
    subprocess.run(
        ["docker", "build", "-t", tag, str(REPO_ROOT)],
        check=True,
    )


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_container(image: str, port: int) -> str:
    name = f"pcv-memcheck-{uuid.uuid4().hex[:8]}"
    print(f"[run] starting container {name} on :{port}", flush=True)
    _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"PORT={port}",
            "-p",
            f"{port}:{port}",
            image,
        ]
    )
    return name


def stop_container(name: str) -> None:
    subprocess.run(
        ["docker", "stop", name],
        capture_output=True,
        text=True,
    )


def wait_for_health(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"container did not become healthy at {url}: {last!r}")


def warmup(base_url: str) -> None:
    print(f"[warmup] {WARMUP_LOOPS}x {list(WARMUP_PATHS)}", flush=True)
    for _ in range(WARMUP_LOOPS):
        for p in WARMUP_PATHS:
            try:
                with urllib.request.urlopen(base_url + p, timeout=5) as r:  # noqa: S310
                    r.read()
            except urllib.error.URLError as exc:
                print(f"[warmup] request failed for {p}: {exc}", file=sys.stderr, flush=True)


def _parse_mem_usage(field: str) -> float:
    """Parse ``docker stats`` MemUsage field 'used / limit' into MB."""
    used = field.split("/", 1)[0].strip()
    num = "".join(c for c in used if c.isdigit() or c == ".")
    unit = used[len(num) :].strip().lower()
    val = float(num)
    if unit.startswith("kib") or unit.startswith("kb"):
        return val / 1024.0
    if unit.startswith("mib") or unit.startswith("mb"):
        return val
    if unit.startswith("gib") or unit.startswith("gb"):
        return val * 1024.0
    if unit.startswith("b"):
        return val / (1024.0 * 1024.0)
    raise ValueError(f"unknown unit in docker stats output: {field!r}")


def sample_memory(container: str) -> float:
    r = _run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.MemUsage}}",
            container,
        ]
    )
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError(f"empty docker stats output for {container}")
    return _parse_mem_usage(out)


def collect_samples(container: str) -> list[Sample]:
    print(f"[sample] {SAMPLE_COUNT}x every {SAMPLE_INTERVAL_S}s", flush=True)
    samples: list[Sample] = []
    start = time.time()
    for i in range(SAMPLE_COUNT):
        mb = sample_memory(container)
        t = time.time() - start
        samples.append(Sample(t=round(t, 2), mb=round(mb, 2)))
        print(f"  sample {i + 1:>2}/{SAMPLE_COUNT}  t={t:6.2f}s  mem={mb:6.2f} MiB", flush=True)
        if i < SAMPLE_COUNT - 1:
            time.sleep(SAMPLE_INTERVAL_S)
    return samples


def run_check(image: str, *, max_mb: float, build: bool) -> Result:
    if build or not _image_exists(image):
        build_image(image)
    port = _free_port()
    container = start_container(image, port)
    try:
        wait_for_health(f"http://127.0.0.1:{port}/healthz", BOOT_TIMEOUT_S)
        warmup(f"http://127.0.0.1:{port}")
        samples = collect_samples(container)
    finally:
        stop_container(container)

    mb_values = [s.mb for s in samples]
    max_mb_observed = max(mb_values)
    mean_mb = sum(mb_values) / len(mb_values)
    passed = max_mb_observed <= max_mb
    return Result(
        image=image,
        samples=samples,
        max_mb=round(max_mb_observed, 2),
        mean_mb=round(mean_mb, 2),
        budget_mb=max_mb,
        passed=passed,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Docker image memory regression check.")
    ap.add_argument("--image", default=DEFAULT_IMAGE, help="image tag to test")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB, help="hard budget in MiB")
    ap.add_argument(
        "--no-build",
        action="store_true",
        help="reuse existing image (build only if missing)",
    )
    ap.add_argument("--json", dest="json_out", help="write metrics JSON here")
    args = ap.parse_args(argv)

    try:
        result = run_check(args.image, max_mb=args.max_mb, build=not args.no_build)
    except subprocess.CalledProcessError as e:
        print(f"[error] subprocess failed: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    print()
    print(f"image:   {result.image}")
    print(f"samples: {len(result.samples)}")
    print(f"max:     {result.max_mb:.2f} MiB")
    print(f"mean:    {result.mean_mb:.2f} MiB")
    print(f"budget:  {result.budget_mb:.2f} MiB")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(asdict(result), indent=2))
        print(f"json:    {args.json_out}")

    if not result.passed:
        print(f"[FAIL] max {result.max_mb:.2f} MiB exceeds budget {result.budget_mb:.2f} MiB")
        return 1

    if result.max_mb >= result.budget_mb * WARN_FRACTION:
        print(
            f"[WARN] max {result.max_mb:.2f} MiB within {int((1 - WARN_FRACTION) * 100)}%"
            f" of budget {result.budget_mb:.2f} MiB — consider investigating"
        )
    else:
        print("[OK] under budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
