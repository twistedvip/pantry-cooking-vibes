# End-to-end (Playwright) tests

These tests drive a real Chromium browser against a live `uvicorn` subprocess
seeded with an isolated SQLite DB. They catch form-submission bugs that the
FastAPI `TestClient` unit tests miss — e.g. a blank `<input type="number">`
that serializes to `max_time=` in the query string.

## One-time setup

```bash
pip install -e ".[dev,e2e]"
playwright install chromium
```

## Run

```bash
# Run only e2e:
pytest -m e2e

# Run headed (watch the browser):
pytest -m e2e --headed

# Run everything (unit + e2e):
pytest -m "e2e or not e2e"
```

Default `addopts = "-m 'not e2e'"` in `pyproject.toml` excludes these from the
normal `pytest` invocation so devs without Playwright browsers installed stay
green. The `e2e` GitHub Actions job (`.github/workflows/ci.yml`) opts in and
runs this suite on every PR.

## Docker image memory check

`test_image_memory.py` is a thin pytest wrapper around
`scripts/check_image_memory.py`. It is double-gated: skipped unless both
`DOCKER_MEM_CHECK=1` and `docker` are present, so a normal `pytest -m e2e`
run on a dev box without Docker stays green. See
[`docs/testing.md`](../../docs/testing.md) for the full memory-budget
workflow and CI integration.

```bash
DOCKER_MEM_CHECK=1 pytest -m e2e tests/e2e/test_image_memory.py -s
```
