<p align="center">
  <img src="docs/images/pantry-cooking-vibes-logo.svg" alt="Pantry Cooking Vibes logo" width="600">
</p>

# Pantry Cooking Vibes

**Tests**

[![CI](https://github.com/twistedvip/pantry-cooking-vibes/actions/workflows/ci.yml/badge.svg)](https://github.com/twistedvip/pantry-cooking-vibes/actions/workflows/ci.yml)

---

Local, single-user meal-planning system where Claude acts as the planning
intelligence. A single SQLite database is the source of truth; a CLI, a
read-mostly FastAPI UI, and an MCP server for Claude Code all read and write
the same file.

The core is **site-agnostic**. Recipes flow in via a documented JSONL
contract (see [`docs/jsonl_contract.md`](docs/jsonl_contract.md)) or via
single-URL JSON-LD import. Site-specific scrapers (Hungryroot, HelloFresh)
live in their own repos, vendored here as git submodules, and register
optional post-processing plugins through Python entry-points.

> **NOTE:** the MCP server is currently untested. Once the web UI is
> complete, the MCP server is the next focus.

## Prerequisites

| Tool | Version | Why |
| ---- | ------- | --- |
| **Python** | 3.11+ | Runtime. `uv` can install a managed Python for you, so a system Python is optional if you use `uv`. |
| **[uv](https://docs.astral.sh/uv/)** | latest | Recommended package/venv manager. Drives `uv sync` + `uv run`, matches CI and `uv.lock`. A plain `pip` flow also works — see [Alternative: pip](#alternative-install-with-pip). |
| **git** | any recent | Clone the repo and fetch the scraper submodules. |
| **Docker** | optional | Only if you want the containerized deployment instead of running locally. See [Run with Docker](#run-with-docker). |
| **Playwright + Chromium** | optional | Only for the end-to-end browser test suite. See [`docs/testing.md`](docs/testing.md). |

### Install uv

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# or, into an isolated environment via pipx / pip
pipx install uv      # preferred
pip install uv
```

The standalone installer drops `uv` in `~/.local/bin` (`%USERPROFILE%\.local\bin`
on Windows) and adds it to your `PATH`. **Open a new terminal** afterwards so
the updated `PATH` takes effect, then confirm:

```bash
uv --version
```

## Quickstart

```bash
# 1. Clone with the scraper submodules (see Recipe scrapers for details)
git clone --recurse-submodules https://github.com/twistedvip/pantry-cooking-vibes
cd pantry-cooking-vibes

# 2. Create the virtualenv and install dependencies (reads uv.lock)
uv sync

# 3. Boot the web UI — initializes data/app.db on first run
uv run meal-cli start                                  # http://127.0.0.1:8000
```

`uv sync` creates a project virtualenv in `.venv/` and installs the locked
dependencies; you never activate it manually — prefix commands with
`uv run`. `meal-cli start` resolves the database path, creates and seeds
`data/app.db` on first run (printing the path before doing so, and seeding
~484 canonical ingredients), then boots the FastAPI UI. Re-running `start`
reuses the existing database.

> For scripted/CI deploys that need an explicit `db-init` step and should
> fail if the database is missing, use `uv run meal-cli serve-web` instead
> (see [`docs/cli.md`](docs/cli.md)).

The fresh database has no recipes yet. Load the bundled demo recipe or
import your own:

```bash
uv run meal-cli ingest data/seed/demo.jsonl --source manual   # demo: chicken fried rice
uv run meal-cli import-url https://www.seriouseats.com/.../recipe
```

From there, ingest JSONL produced by a scraper
([Recipe scrapers](#recipe-scrapers-hungryroot--hellofresh) /
[`docs/jsonl_contract.md`](docs/jsonl_contract.md)) or keep importing
single URLs.

### Alternative: install with pip

If you'd rather not use `uv`, a standard venv + editable install works the
same way (the `meal-cli` entry point is identical):

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"              # omit [dev] for runtime-only

meal-cli start                       # http://127.0.0.1:8000
```

With this flow, drop the `uv run` prefix from every command below (the
activated venv puts `meal-cli`, `pytest`, etc. on `PATH` directly).

### Run with Docker

A pre-built multi-arch image is published to GHCR on every release tag.
The bundled `docker-compose.yml` persists the database in a named volume:

```bash
docker compose pull && docker compose up -d        # http://127.0.0.1:8000
PCV_PORT=9000 docker compose up -d                 # override the port
```

See the header of [`docker-compose.yml`](docker-compose.yml) for Portainer
import, release pinning (`PCV_TAG`), build-from-source, and backup notes.

## Running tests & checks

```bash
uv run pytest                 # unit + integration (e2e excluded by default)
uv run ruff check             # lint
uv run ty check src           # type-check
```

The end-to-end Playwright suite is opt-in and needs browsers installed:

```bash
uv sync --extra e2e
uv run playwright install chromium
uv run pytest -m e2e
```

Full details — fixtures, the unit/e2e split, the Docker memory check — are in
[`docs/testing.md`](docs/testing.md).

## Recipe scrapers (Hungryroot & HelloFresh)

Site-specific scrapers live in their own repositories and are vendored here
as git submodules under `scrapers/`:

| Submodule | Source | Plugin name |
| --------- | ------ | ----------- |
| `scrapers/pantry-cooking-vibes-hungryroot` | Hungryroot public API (~64k recipes) | `hungryroot` |
| `scrapers/pantry-cooking-vibes-hellofresh` | HelloFresh sitemap + JSON-LD | `hellofresh` |

### 1. Fetch the submodules

If you cloned with `--recurse-submodules`, they're already present. Otherwise,
or if `scrapers/<name>/` is empty, initialize them:

```bash
git submodule update --init --recursive
```

### 2. How a scraper feeds the core

Each scraper is its own Python package that registers a `RecipeImporter`
entry-point in the `pantry_cooking_vibes.importers` group. The flow is:

1. **Scrape** → the scraper writes *raw* JSONL (its own API/site shape).
2. **Ingest** → `meal-cli ingest <raw.jsonl> --source <name> --plugin <name>`
   loads the plugin, whose `post_process()` adapts each raw line into the
   [JSONL contract](docs/jsonl_contract.md), then UPSERTs into core.

Concrete commands for each step are below. Each scraper's own README has the
full detail (adapter behavior, editorial-marker stripping, endpoints):

- [`scrapers/pantry-cooking-vibes-hungryroot/README.md`](scrapers/pantry-cooking-vibes-hungryroot/README.md)
- [`scrapers/pantry-cooking-vibes-hellofresh/README.md`](scrapers/pantry-cooking-vibes-hellofresh/README.md)

### 3. Make a scraper's plugin visible to `meal-cli`

The plugin must be installed into the **same environment** as `meal-cli`.
Install it editable with `--no-deps` so it doesn't pull a second copy of the
core package (core, `requests`, and `beautifulsoup4` are already in the env):

```bash
uv pip install --no-deps -e scrapers/pantry-cooking-vibes-hungryroot
# or: scrapers/pantry-cooking-vibes-hellofresh
```

Verify the entry-point registered:

```bash
uv run python -c "from importlib.metadata import entry_points as e; print([x.name for x in e(group='pantry_cooking_vibes.importers')])"
# -> ['hungryroot']
```

> **Heads-up:** `uv sync` reconciles the venv against `uv.lock` and will
> **remove** a plugin installed this way (it isn't a locked dependency of the
> site-agnostic core). Re-run the `uv pip install --no-deps -e …` line after
> any `uv sync`. A plain `uv run …` does **not** remove it.

### 4. Scrape recipes

The scrapers expose Python functions, not console scripts, so start a scrape
with `python -c` (or import them from your own script). Each writes *raw*
JSONL to **`scrapers/data/raw/<name>/recipes.jsonl`** plus a resumable state
file beside it — a killed scrape resumes where it left off on the next run.

**Hungryroot** (`scrape_pairings`, ~69k recipes, ≈15 min at the default 1 s
per-page delay):

```bash
# Full scrape -> scrapers/data/raw/hungryroot/recipes.jsonl
uv run python -c "from pantry_cooking_vibes_hungryroot.scraper import scrape_pairings; scrape_pairings()"

# Quick test scrape — first 2 pages (~1000 records)
uv run python -c "from pantry_cooking_vibes_hungryroot.scraper import scrape_pairings; scrape_pairings(max_pages=2)"
```

**HelloFresh** (`scrape_recipes`, walks the sitemap). The bundled
`run_filtered_scrape.py` skips pre-2016 URLs that ship empty JSON-LD and
honors `HF_MAX_RECIPES` (cap, 0 = all) and `HF_SLEEP` (per-request delay):

```bash
# Full filtered scrape -> scrapers/data/raw/hellofresh/recipes.jsonl
uv run python scrapers/pantry-cooking-vibes-hellofresh/scripts/run_filtered_scrape.py

# Capped test scrape (2 recipes)
HF_MAX_RECIPES=2 uv run python scrapers/pantry-cooking-vibes-hellofresh/scripts/run_filtered_scrape.py
# PowerShell: $env:HF_MAX_RECIPES=2; uv run python scrapers/pantry-cooking-vibes-hellofresh/scripts/run_filtered_scrape.py
```

### 5. Ingest the scraped recipes

```bash
uv run meal-cli ingest scrapers/data/raw/hungryroot/recipes.jsonl --source hungryroot --plugin hungryroot
```

> **Expect fewer recipes than you scraped.** Ingest **skips records with no
> `image_url`** and drops near-duplicates by default. A full Hungryroot run of
> 68,987 raw pairings lands ~44,000 recipes (≈18k dropped for missing images,
> ≈7k de-duplicated). Pass `--no-dedup` to keep every record, and `--verbose`
> to watch validation/dedup/write progress on a large file.

If the plugin package can't be installed in the target environment, each
scraper also ships a `*-transform` console script (`hungryroot-transform`,
`hellofresh-transform`) that pre-converts raw JSONL into contract-shaped
JSONL you can ingest **without** `--plugin`. See the scraper READMEs.

## Configuration

| Env var | Effect |
| ------- | ------ |
| `PANTRY_COOKING_VIBES_DB` | Database path. Overridden per-command by `--db PATH`; default `data/app.db`. |
| `PANTRY_COOKING_VIBES_UA` | User-Agent for `import-url`, if a site 403s the default. |

## Feature map

| Feature                          | Entry point                       | Notes                                   |
| -------------------------------- | --------------------------------- | --------------------------------------- |
| Browse recipes (FTS + filters)   | `GET /recipes`                    | tag/time/favorites filters              |
| Recipe detail + pantry highlight | `GET /recipes/{id}`               | shows which ingredients you already own |
| Favorite / unfavorite            | `POST /recipes/{id}/favorite`     | `recipe_favorites` table                |
| Pantry (read+write)              | `GET/POST /pantry`                | only write surface in the web UI        |
| Meal plans                       | `GET /plans`, `/plans/{id}`       | browse-only in web; edit via MCP        |
| Shopping list                    | `GET /plans/{id}/shopping`        | qualitative (no quantity math, yet)     |
| Ingest JSONL (any source)        | `meal-cli ingest <file> --source` | UPSERT on `(source, source_id)`         |
| Single URL import (JSON-LD)      | `meal-cli import-url <url>`       | schema.org Recipe                       |
| List sources                     | `meal-cli list-sources`           | distinct `recipes.source` + counts      |
| Backfill ingredient mappings     | `meal-cli normalize-recipes`      | fuzzy-match → `canonical_ingredients`   |
| MCP server (Claude Code)         | `meal-cli serve-mcp`              | 13 tools over stdio                     |
| DB backup                        | `meal-cli db-backup <dest>`       | SQLite online backup API                |

## Plugin model

Scrapers produce JSONL conforming to the contract; core's `ingest` does
the heavy lifting. If a scraper needs site-specific cleanup before
validation (editorial-marker stripping, unit canonicalisation), it
registers a class on entry-point group `pantry_cooking_vibes.importers`
and you invoke it with `--plugin <name>`. Without `--plugin`, plain JSONL
is used.

See [`docs/importers.md`](docs/importers.md) and
[`docs/jsonl_contract.md`](docs/jsonl_contract.md).

## Docs

- [`docs/architecture.md`](docs/architecture.md) — components, data flow, layering rules
- [`docs/database.md`](docs/database.md) — schema tour, migrations, FTS5
- [`docs/web.md`](docs/web.md) — routes, templates, a worked favorites call flow
- [`docs/cli.md`](docs/cli.md) — every `meal-cli` subcommand with examples
- [`docs/mcp.md`](docs/mcp.md) — MCP tool surface for Claude
- [`docs/importers.md`](docs/importers.md) — JSONL ingest, URL import, plugin contract
- [`docs/jsonl_contract.md`](docs/jsonl_contract.md) — wire format spec for scrapers
- [`docs/testing.md`](docs/testing.md) — fixtures, unit vs e2e, running the suite

## Layout

```
src/pantry_cooking_vibes/
  cli.py                # typer CLI (every meal-cli command lives here)
  db.py                 # connection, apply_schema, run_migrations, init_db
  models.py             # pydantic DTOs (Recipe, RecipeRecord)
  importers/            # url_import, jsonl_ingest, registry, normalize, _utils
  mcp_server/           # tools.py (pure functions) + server.py (FastMCP wrapper)
  web/                  # FastAPI app, routes, Jinja templates, static assets
  _assets/              # packaged sql/csv shipped in the wheel
    schema.sql          # idempotent baseline schema
    migrations/         # *.sql applied after schema, tracked in schema_migrations (empty at v0.1.0)
    canonical_seed.csv  # canonical_ingredients seed data
data/
  app.db                # default SQLite database (runtime, gitignored, not packaged)
  seed/demo.jsonl       # one demo recipe shipped with core
scrapers/               # site-specific scraper submodules (Hungryroot, HelloFresh)
tests/
  test_*.py             # unit/integration tests (default pytest target)
  e2e/                  # Playwright tests, opt-in via `-m e2e`
```

## Conventions

- **SQLite is the source of truth.** `models.py` mirrors the schema but is not
  the store; every read/write goes through `db.connect()`.
- **Idempotence everywhere.** `init_db`, `run_migrations`, and ingest
  (UPSERT on `(source, source_id)`) all safely re-run.
- **Pure tool functions.** `mcp_server/tools.py` functions take `db_path=` so
  both the MCP server and the FastAPI routes call the same code with no
  process-level coupling.
- **Site-agnostic core.** No brand-specific scraping code in this repo.
  Scrapers live in their own repos and either dump conforming JSONL or
  register an entry-point plugin.
