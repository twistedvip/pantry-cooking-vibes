# Pantry Cooking Vibes

Local, single-user meal-planning system where Claude acts as the planning
intelligence. A single SQLite database is the source of truth; a CLI, a
read-mostly FastAPI UI, and an MCP server for Claude Code all read and write
the same file.

The core is **site-agnostic**. Recipes flow in via a documented JSONL
contract (see [`docs/jsonl_contract.md`](docs/jsonl_contract.md)) or via
single-URL JSON-LD import. Site-specific scrapers live in their own
repos and register optional post-processing plugins through Python
entry-points.

## Quickstart

```bash
uv sync
meal-cli db-init                                      # create data/app.db
meal-cli ingest data/seed/demo.jsonl --source manual  # load demo recipe
meal-cli serve-web                                    # http://127.0.0.1:8000
```

`data/seed/demo.jsonl` ships one recipe (chicken fried rice) so a fresh
install isn't empty. From there, ingest more JSONL produced by a scraper
of your choice, or import single recipes by URL:

```bash
meal-cli import-url https://www.seriouseats.com/.../recipe
```

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
db/
  schema.sql            # idempotent baseline schema
  migrations/           # *.sql files applied after schema, tracked in schema_migrations
data/
  app.db                # default SQLite database
  seed/canonical_seed.csv  # canonical_ingredients seed data
  seed/demo.jsonl       # one demo recipe shipped with core
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
