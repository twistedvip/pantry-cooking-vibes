# Meal Planner

Local, single-user meal-planning system where Claude acts as the planning
intelligence. A single SQLite database is the source of truth; a CLI, a
read-mostly FastAPI UI, and an MCP server for Claude Code all read and write
the same file.

## Quickstart

```bash
uv sync
meal-cli db-init           # create data/app.db, apply schema + migrations, seed canonicals
meal-cli serve-web         # browse at http://127.0.0.1:8000
```

A pre-populated catalog is optional; for an empty DB the pantry still works
and you can import one recipe at a time via `meal-cli url-import <url>`.

## Feature map

| Feature                          | Entry point                     | Notes                                   |
| -------------------------------- | ------------------------------- | --------------------------------------- |
| Browse recipes (FTS + filters)   | `GET /recipes`                  | tag/time/favorites filters              |
| Recipe detail + pantry highlight | `GET /recipes/{id}`             | shows which ingredients you already own |
| Favorite / unfavorite            | `POST /recipes/{id}/favorite`   | `recipe_favorites` table                |
| Pantry (read+write)              | `GET/POST /pantry`              | only write surface in the web UI        |
| Meal plans                       | `GET /plans`, `/plans/{id}`     | browse-only in web; edit via MCP        |
| Shopping list                    | `GET /plans/{id}/shopping`      | qualitative (no quantity math, yet)     |
| Scrape Hungryroot                | `meal-cli hr-scrape` / `-products` | resumable paginated scrapes          |
| Normalize HR products            | `meal-cli normalize`            | fuzzy-match to canonical ingredients    |
| Import HR pairings               | `meal-cli hr-import`            | JSONL → DB, upserts                     |
| Import URL (JSON-LD)             | `meal-cli url-import <url>`     | schema.org Recipe                       |
| MCP server (for Claude Code)     | `meal-cli serve-mcp`            | 13 tools over stdio                     |
| DB backup                        | `meal-cli db-backup <dest>`     | SQLite online backup API                |

## Docs

- [`docs/architecture.md`](docs/architecture.md) — components, data flow, layering rules
- [`docs/database.md`](docs/database.md) — schema tour, migrations, FTS5
- [`docs/web.md`](docs/web.md) — routes, templates, a worked favorites call flow
- [`docs/cli.md`](docs/cli.md) — every `meal-cli` subcommand with examples
- [`docs/mcp.md`](docs/mcp.md) — MCP tool surface for Claude
- [`docs/importers.md`](docs/importers.md) — Hungryroot scrape/import + URL import
- [`docs/testing.md`](docs/testing.md) — fixtures, unit vs e2e, running the suite

## Layout

```
src/meal_planner/
  cli.py                # typer CLI (every meal-cli command lives here)
  db.py                 # connection, apply_schema, run_migrations, init_db
  models.py             # pydantic DTOs (mostly documentation; DB is source of truth)
  importers/            # hungryroot scrape, hr/url import, normalization
  mcp_server/           # tools.py (pure functions) + server.py (FastMCP wrapper)
  web/                  # FastAPI app, routes, Jinja templates, static assets
db/
  schema.sql            # idempotent baseline schema
  migrations/           # *.sql files applied after schema, tracked in schema_migrations
data/
  app.db                # default SQLite database
  raw/hungryroot/       # scraped JSONL + resume-state files
  seed/canonical_seed.csv  # canonical_ingredients seed data
tests/
  test_*.py             # unit/integration tests (default pytest target)
  e2e/                  # Playwright tests, opt-in via `-m e2e`
```

## Conventions

- **SQLite is the source of truth.** `models.py` mirrors the schema but is not
  the store; every read/write goes through `db.connect()`.
- **Idempotence everywhere.** `init_db`, `run_migrations`, imports (upsert on
  `(source, source_id)`), and normalization all safely re-run.
- **Pure tool functions.** `mcp_server/tools.py` functions take `db_path=` so
  both the MCP server and the FastAPI routes call the same code with no
  process-level coupling.
