# Architecture

## Components

```
                       +-----------------------+
                       |  data/app.db (SQLite) |
                       +-----------+-----------+
                                   ^
            read/write             |             read/write
        +----------+----------+----+------+----+----------+
        |          |          |           |    |          |
        | pantry_cooking_vibes.cli  |  mcp_server.tools (pure) |
        |    (Typer CLI)      |                           |
        |          |          +---------+-----------------+
        |          |                    ^
        |          v                    |
        |   importers.*            web.routes.*  <-- Jinja2Templates
        |   (jsonl_ingest /        (FastAPI app)
        |    url_import /              ^
        |    normalize /               |
        |    registry)            mcp_server.server
        |                         (FastMCP stdio)
        +------------------------------+
```

Every component talks to the same SQLite file via
`pantry_cooking_vibes.db.connect`. The only coupling between components is
shared table shape — no RPC, no shared process.

## Layering rules

1. **`db.py` is the base.** Connection helpers, schema application, migration
   runner, seed loader. Depends on nothing but stdlib + `pathlib`.
2. **`mcp_server/tools.py` is the shared SQL layer.** Pure functions that take
   `db_path=` and return plain dicts. Called by the FastAPI routes *and* by
   the MCP server. There is no "service layer" — tools.py is it.
3. **`web/routes/` is presentation only.** Routes validate form/query input,
   call into `tools.py`, and hand dicts to Jinja templates. Two escape
   hatches: `home.py` runs small count queries directly, and shopping uses
   `connect()` for a single cross-table read. No route writes SQL that
   duplicates `tools.py`.
4. **`importers/` and `cli.py` are operational.** Importers transform external
   inputs (JSONL files, URLs) into rows the app layer reads; the CLI is the
   user-facing wrapper. Neither is imported by the web or MCP paths.

The rule of thumb: if a new feature needs SQL, add it to `tools.py` first and
call that from both `web/routes/` and `mcp_server/server.py`. Don't split the
implementation across them.

## Data flow: recipe → plate

The core is **site-agnostic**. No brand-specific scraper code lives in this
repo. Recipes enter via two surfaces:

1. **JSONL ingest** — `meal-cli ingest <file> --source <name>`. External
   scrapers (each in their own repo) emit JSONL conforming to
   [`jsonl_contract.md`](jsonl_contract.md). Core validates each line via
   the `RecipeRecord` Pydantic model and UPSERTs into `recipes` /
   `recipe_tags` / `recipe_ingredients` keyed on `(source, source_id)`.
   Tags + ingredients are replaced wholesale per recipe = re-imports are
   full refreshes.
2. **URL import** — `meal-cli import-url <url>`. Single-recipe shortcut
   that fetches a page, extracts schema.org Recipe JSON-LD, and UPSERTs
   with `source='url'`. Used for one-off additions; same DB shape as
   ingest.

Then the human + Claude loop:

3. **Normalize** — `meal-cli normalize-recipes` fuzzy-matches
   `recipe_ingredients.original_text` against `canonical_ingredients`,
   writing `canonical_id` for high-confidence matches and queuing the
   rest in `ingredient_mapping_queue`. Curator resolves via
   `review-mappings` + `approve-mapping` (then `apply-text-mappings`
   back-fills approved rows).
4. **Pantry** — User adds canonical ingredients they own via
   `POST /pantry/add`.
5. **Plan** — Claude (via MCP) creates a `meal_plans` row and attaches
   recipes with `add_recipe_to_plan`.
6. **Shop** — `compute_shopping_list(plan_id)` joins plan items →
   recipe_ingredients → canonical_ingredients, subtracts the pantry, and
   returns `{needed, covered_by_pantry, uncategorized}`.

## Plugin discovery

Site-specific scrapers may register a Python class on entry-point group
`pantry_cooking_vibes.importers`. The class implements a `post_process`
hook that mutates raw records before validation:

```python
class ExampleImporter:
    name = "example"
    version = "1.0.0"

    def post_process(self, records: list[dict]) -> list[dict]:
        return [r for r in records if r.get("name")]
```

Discovery happens via `importlib.metadata.entry_points`; see
`importers/registry.py`. Without `--plugin`, plain JSONL ingestion is
used.

## Request lifecycle (web)

```
browser
  │  GET /recipes?q=soup
  v
uvicorn → FastAPI app (app_factory.py)
  │  resolves db_path via Depends(get_db_path)
  v
routes/recipes.py::list_recipes
  │  parses form strings, calls tools.search_recipes(...)
  v
mcp_server/tools.py
  │  opens connect(db_path), runs FTS5 + filters SQL
  v
Jinja (via web/deps.py::render)
  │  recipes/list.html extends base.html
  v
HTML response
```

Tests override `get_db_path` via `app.dependency_overrides` — see
[`docs/testing.md`](testing.md).

## Process topology

| Process            | Transport | Launched by          |
| ------------------ | --------- | -------------------- |
| FastAPI web UI     | HTTP      | `meal-cli serve-web` |
| MCP server         | stdio     | `meal-cli serve-mcp` |
| One-shot CLI tasks | n/a       | `meal-cli <cmd>`     |

These processes are independent. Concurrency between them relies on SQLite
WAL mode (enabled in `db.get_connection`) and a 5-second busy timeout.

## Idempotence invariants

- `init_db`, `apply_schema`, and `run_migrations` are safe to re-run at any
  time. `serve-web` runs migrations on startup so a stale DB self-heals.
- Recipe ingest UPSERTs on `(source, source_id)`; tags and ingredients are
  replaced wholesale per recipe.
- Normalization upserts into `ingredient_mapping_queue`; approved rows
  retain their `canonical_id` link across re-runs.
