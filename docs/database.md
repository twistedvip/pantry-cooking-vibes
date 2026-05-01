# Database

SQLite file at `data/app.db`, WAL mode, foreign keys enforced. The full
baseline lives in [`db/schema.sql`](../db/schema.sql); additive changes ship
as migrations in [`db/migrations/`](../db/migrations).

## Tables (brief)

| Table                      | Purpose                                                      |
| -------------------------- | ------------------------------------------------------------ |
| `canonical_ingredients`    | Normalized ingredients (e.g. `broccoli`) with aliases, category. Seeded from CSV. |
| `recipes`                  | One row per recipe. `source` is free-form text registered by ingest (validated `^[a-z][a-z0-9-]*$`). Unique on `(source, source_id)`. |
| `recipes_fts`              | FTS5 virtual table mirroring `recipes.name` + `instructions_md`. Kept in sync via triggers. |
| `recipe_ingredients`       | Per-recipe ingredient rows. `canonical_id` is nullable — unmapped ingredients keep their `original_text`. |
| `recipe_tags`              | `(recipe_id, tag)` many-to-many.                             |
| `recipe_favorites`         | `recipe_id` PK. Added in migration `001_recipe_favorites.sql`. |
| `pantry`                   | Items the user owns, keyed by `canonical_id`.                |
| `meal_plans`               | One row per plan; `week_of` ISO date, `status` draft/confirmed. |
| `meal_plan_items`          | Recipes attached to a plan with optional `day` / `meal_slot`. |
| `shopping_list_items`      | Reserved for future quantity-aware shopping lists (unused in v1). |
| `ingredient_mapping_queue` | Proposed mappings from raw ingredient strings to canonical ingredients. Curator approves/rejects. |
| `schema_migrations`        | Migration bookkeeping (filename → applied_at).               |

## `recipes.source`

Free-form text. Set at ingest time (`meal-cli ingest <file> --source NAME`)
and validated against `^[a-z][a-z0-9-]*$` by `pantry_cooking_vibes.models.SOURCE_NAME_RE`.

History: the column originally had a CHECK constraint enumerating known
sources. Migration `003_recipes_source_freeform.sql` rebuilt the table
without that constraint so external scrapers can register new sources
without core changes.

`tools.list_recipe_sources` (used by `meal-cli list-sources` and the web)
runs `SELECT DISTINCT source FROM recipes` so new values surface
automatically.

## FTS5 wiring

`recipes_fts` is a `content='recipes'` FTS5 table. Three triggers (`recipes_ai`,
`recipes_ad`, `recipes_au`) keep it in sync with writes to `recipes.name` and
`recipes.instructions_md`. Queries use:

```sql
SELECT r.* FROM recipes_fts f JOIN recipes r ON r.id = f.rowid
WHERE recipes_fts MATCH ?
ORDER BY f.rank, r.rating DESC NULLS LAST
```

This means any write path that bypasses the `recipes` triggers (bulk SQL
outside the defined triggers) would silently desync the index. All current
write paths use single-row INSERT/UPDATE.

## Applying schema and migrations

`pantry_cooking_vibes.db.init_db` is the single entry point. It does three
things, idempotently:

1. `apply_schema(conn)` — runs `db/schema.sql` (all `CREATE IF NOT EXISTS`).
2. `run_migrations(conn)` — applies any `*.sql` under `db/migrations/` not
   yet recorded in `schema_migrations`, in filename order.
3. `seed_canonical_ingredients(conn)` — loads `data/seed/canonical_seed.csv`
   into `canonical_ingredients` via `INSERT OR IGNORE`.

`serve-web` calls `run_migrations` during startup so a DB that predates a new
migration self-heals before the first request.

## Adding a migration

1. Create `db/migrations/NNN_short_name.sql` (three-digit prefix, sorted by
   filename). Use `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` — migrations
   should not require the previous state to be absent.
2. Reference any new tables/columns from `tools.py` and templates.
3. Run `meal-cli db-init` locally to apply.
4. Add a regression test if it's a table referenced by a hot path (see
   `test_serve_web_applies_pending_migrations`).

## Connection semantics

```python
from pantry_cooking_vibes.db import connect

with connect(db_path) as conn:
    conn.execute("INSERT ...", params)
# commit on clean exit, rollback on exception, close in finally
```

`get_connection` sets `journal_mode=WAL`, `foreign_keys=ON`,
`synchronous=NORMAL`, `busy_timeout=5000`. `row_factory=sqlite3.Row` so you
can do `row["name"]`.

## Backups

```bash
meal-cli db-backup ./db_backups/        # directory → timestamped filename
meal-cli db-backup /tmp/snapshot.db     # file path → exact filename
```

Uses `sqlite3.Connection.backup()` (online backup API), safe to run while
`serve-web` / `serve-mcp` are live.
