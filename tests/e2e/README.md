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
