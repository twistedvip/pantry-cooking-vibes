# Testing

Stack: `pytest` for unit/integration, Playwright for end-to-end. Tests live
in [`tests/`](../tests). Linter: `ruff check`. Type-checker: `ty check`.

## Running

```bash
uv run pytest                       # default: unit + integration (excludes e2e)
uv run pytest tests/test_web.py -v  # one file
uv run pytest -k favorite           # by name pattern
uv run pytest -m e2e                # opt into the Playwright suite
uv run pytest -m "e2e or not e2e"   # everything
uv run ruff check                   # lint
uv run ty check src                 # type-check
```

`pyproject.toml` sets `addopts = "-m 'not e2e'"` so the default run stays fast
on machines without Playwright browsers installed.

## File map

| File                              | Scope                                                            |
| --------------------------------- | ---------------------------------------------------------------- |
| `tests/conftest.py`               | `db_path` + `seeded_db_path` fixtures (see below).               |
| `tests/test_db.py`                | Schema apply, migration machinery, seed loader.                  |
| `tests/test_mcp_tools.py`         | Pure `tools.py` functions — framework-free.                      |
| `tests/test_url_import.py`        | JSON-LD extraction, `parse_recipe`, duration parsing, end-to-end import with stubbed HTML. |
| `tests/test_jsonl_ingest.py`      | JSONL contract validation, ingest UPSERT semantics, plugin post-process hook. |
| `tests/test_normalize.py`         | Fuzzy matching, threshold behavior, alias collisions, mapping upsert. |
| `tests/test_web.py`               | FastAPI routes via `TestClient`, favorites, pantry R/W, CLI-wiring regressions. |
| `tests/e2e/`                      | Playwright browser tests against a live uvicorn subprocess.      |
| `tests/fixtures/`                 | Captured HTML pages for URL-import tests.                        |

(File names approximate — run `ls tests/` for the live list.)

## Shared fixtures (`tests/conftest.py`)

```python
@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "app.db"
    init_db(db_path=db)      # schema + migrations + seed
    return db

@pytest.fixture
def seeded_db_path(db_path: Path) -> Path:
    # 2 recipes ("Broccoli Stir Fry", "Broccoli Soup"),
    # one tagged+mapped ingredient, one unmapped ingredient,
    # 1 pantry item (broccoli).
    return db_path  # after INSERTs
```

`db_path` is the workhorse: a fresh DB per test, fully migrated, seed
loaded. `seeded_db_path` adds just enough rows to cover the web UI's
default views without introducing test cross-talk.

Because `init_db` runs migrations inside the fixture, every test
naturally runs against the latest schema — which means a regression of
the form "code references a new table that isn't in `schema.sql`" will
be caught as soon as the test hits it.

At v0.1.0 the migrations directory is empty (the 001–006 sequence was
collapsed into the baseline schema), so `run_migrations` is a no-op
during fixture setup. The `serve-web` bootstrap still calls it on
startup as a self-heal step for older deployed DBs that predate a
future migration; `test_serve_web_applies_pending_migrations` in
`test_web.py` exercises that path by injecting a synthetic pending
migration via `monkeypatch.setattr(db, "_MIGRATIONS_DIR", ...)`.

## Testing the FastAPI app

```python
from fastapi.testclient import TestClient
from pantry_cooking_vibes.web.app import create_app

def test_recipes_list(seeded_db_path):
    client = TestClient(create_app(db_path=seeded_db_path))
    r = client.get("/recipes")
    assert r.status_code == 200
```

`create_app(db_path=...)` pins the DB via `app.dependency_overrides[get_db_path]`.
This is the one correct way to target a test DB — don't set
`PANTRY_COOKING_VIBES_DB` in tests, because `app_factory.py` resolves it
at import time.

## Testing CLI commands

```python
from typer.testing import CliRunner
from pantry_cooking_vibes.cli import app as cli_app

def test_db_backup_round_trip(tmp_path, seeded_db_path):
    dest = tmp_path / "nested" / "backup.db"
    result = CliRunner().invoke(
        cli_app, ["db-backup", str(dest), "--db", str(seeded_db_path)]
    )
    assert result.exit_code == 0, result.output
```

For `serve-web`, patch `uvicorn.run` so the command's bootstrap path
runs but no port is bound:

```python
def test_serve_web_applies_pending_migrations(tmp_path, monkeypatch):
    invoked = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: invoked.setdefault("ran", True))
    ...
```

## Testing URL import without the network

`url_import.import_url(url, html=...)` skips the fetch. Use the
captured pages under `tests/fixtures/` and the `html=` kwarg rather
than any `requests_mock`-style stubbing.

## Testing JSONL ingest

`ingest_jsonl(jsonl_path, source, db_path=...)` reads from a file path,
so tests typically write a small JSONL into `tmp_path`:

```python
def test_ingest_basic(tmp_path, db_path):
    jsonl = tmp_path / "in.jsonl"
    jsonl.write_text(json.dumps({
        "source_id": "abc",
        "name": "Test Recipe",
        "ingredients": [{"original_text": "1 cup rice"}],
    }) + "\n", encoding="utf-8")
    stats = ingest_jsonl(jsonl, "test-source", db_path=db_path)
    assert stats["recipes"] == 1
```

For plugin tests, register a fake plugin via
`monkeypatch.setattr("pantry_cooking_vibes.importers.registry.discover_plugins", ...)`
so you don't need to install a real entry-point package.

## End-to-end (Playwright)

See [`tests/e2e/README.md`](../tests/e2e/README.md). The suite boots a
real uvicorn subprocess against an isolated DB and drives a headless
Chromium. It catches DOM-level regressions that `TestClient` can't —
e.g. browser form serialization for blank number inputs.

First-time setup:

```bash
pip install -e ".[dev,e2e]"
playwright install chromium
```

Default-excluded from local `pytest`, but the CI workflow has a dedicated
`e2e` job (`.github/workflows/ci.yml`) that installs `[e2e]`, caches the
Playwright browsers on `uv.lock` hash, and runs `pytest -m e2e` on every
PR alongside the unit-test matrix.

## What to test for a new feature

Rough heuristic:
- New SQL in `tools.py` → test in `test_mcp_tools.py` against
  `db_path`.
- New route → test in `test_web.py` via `TestClient`.
- New migration referenced by an existing hot path → add a regression
  test that creates a "pre-migration" DB (apply schema only) and
  exercises the code to confirm the auto-apply path works.
- New scraper/parser behavior → capture a real page or payload into
  `tests/fixtures/` and assert on the parsed output.
- New JSONL contract field → add a `test_jsonl_ingest.py` case
  exercising both presence and absence of the field.
