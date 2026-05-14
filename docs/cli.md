# CLI

All commands are Typer subcommands of `meal-cli`, defined in
[`src/pantry_cooking_vibes/cli.py`](../src/pantry_cooking_vibes/cli.py).
Run `meal-cli --help` for the live list; this page documents each one
with a usage example and notes on failure modes.

Every command that touches the DB accepts `--db PATH` to target a file
other than `data/app.db`.

## Database

### `meal-cli db-init`

Apply schema, run migrations, seed canonical ingredients.

```bash
meal-cli db-init
# Database initialized: .../data/app.db
# Canonical ingredients seeded: 0
# Total canonical_ingredients rows: 330
```

Safe to re-run. Idempotent at every level — schema uses `CREATE IF NOT
EXISTS`, migrations are tracked in `schema_migrations`, and seed uses
`INSERT OR IGNORE`.

### `meal-cli db-backup <dest>`

Back up the SQLite file using the online backup API.

```bash
meal-cli db-backup ./db_backups/            # timestamped filename inside dir
meal-cli db-backup /tmp/before_upgrade.db   # exact path
```

A trailing separator or an existing directory triggers the
"timestamped filename" mode. If `<dest>`'s parent doesn't exist, it's
created. Exits 1 if the source DB is missing.

## Ingest

### `meal-cli ingest <jsonl_path>`

Generic JSONL ingest. The wire format is documented in
[`docs/jsonl_contract.md`](jsonl_contract.md). Each line is validated as
a `RecipeRecord`; valid records UPSERT into `recipes` /
`recipe_ingredients` / `recipe_tags` keyed on `(source, source_id)`.

```bash
meal-cli ingest data/seed/demo.jsonl --source manual
# ingest done (source=manual):
#   processed   : 1
#   recipes     : 1
#   ingredients : 8
#   tags        : 3
#   skipped     : 0
# normalize done:
#   distinct texts : 8
#   auto-approved  : 6
#   proposed       : 1
#   no_match       : 1
#   rows updated   : 6
```

Flags:

- `--source NAME` — required name of the source. Must match
  `^[a-z][a-z0-9-]*$`. If omitted, the command lists existing sources
  (from `SELECT DISTINCT source FROM recipes`) and prompts.
- `--plugin NAME` — optional plugin from entry-point group
  `pantry_cooking_vibes.importers`. The plugin's `post_process(records)`
  hook runs before validation. See
  [`docs/jsonl_contract.md`](jsonl_contract.md#plugin-escape-hatch).
- `--quiet` — suppress per-record progress output.
- `--normalize` / `--no-normalize` — after a successful ingest, run the
  same fuzzy-match backfill as [`normalize-recipes`](#meal-cli-normalize-recipes)
  over the new recipe ingredients, and append its stats to the output.
  On by default. Skipped on `--dry-run` and when no recipes were
  written (e.g. every line invalid); the output says so explicitly.

Failure modes:
- JSONL file missing → exit 1 with `JSONL not found: <path>`.
- Invalid `--source` → exit 1 with the regex hint.
- Per-line validation errors are tallied as `skipped` (not fatal).

Tags + ingredients are deleted and re-inserted wholesale per recipe, so
re-ingesting the same JSONL is a full refresh.

### `meal-cli import-url <url>`

Fetch a single URL, extract schema.org Recipe JSON-LD, UPSERT into the
`recipes` table with `source='url'`.

```bash
meal-cli import-url https://www.seriouseats.com/.../recipe
```

Failure modes:
- No JSON-LD Recipe on the page → exit 1 with `error: no schema.org
  Recipe found at <url>`.
- Network failure → exit 2 with the underlying `requests` message.

Override the User-Agent with `PANTRY_COOKING_VIBES_UA=<string>` if a
site 403s the default Chrome fingerprint.

### `meal-cli list-sources`

Print distinct `recipes.source` values with recipe counts.

```bash
meal-cli list-sources
# SOURCE  RECIPES
# -------------
# manual  1
# url     7
```

Empty DB prints `No recipes in database.`

## Normalization

These commands operate on `recipe_ingredients.original_text` and the
`ingredient_mapping_queue` table. Run them after ingest to back-fill
`canonical_id` so shopping lists can categorize ingredients.

### `meal-cli normalize-recipes`

Backfill `recipe_ingredients.canonical_id` by fuzzy-matching
`original_text` against `canonical_ingredients` (`rapidfuzz.WRatio`).

```bash
meal-cli normalize-recipes
# normalize-recipes done:
#   distinct texts : 312
#   auto-approved  : 198
#   proposed       : 87
#   no_match       : 27
#   rows updated   : 482
```

Thresholds:
- ≥ 90 → auto-write `canonical_id`
- 70–89 → queued as `proposed` (human review)
- < 70 → stored as `no_match` (still queued so reviewers see what was scanned)

Skips rows that already have a `canonical_id`.

### `meal-cli review-mappings`

Print pending `ingredient_mapping_queue` rows for human review.

```bash
meal-cli review-mappings --limit 50
```

### `meal-cli approve-mapping <id>` / `reject-mapping <id>`

Resolve one queue row. `--canonical-id` on `approve-mapping` overrides
the proposed canonical when you disagree with the fuzzy match.

```bash
meal-cli approve-mapping 42
meal-cli approve-mapping 42 --canonical-id 17
meal-cli reject-mapping 42
```

### `meal-cli apply-text-mappings`

After approving rows in the queue, this command back-fills any
`recipe_ingredients` whose `original_text` matches an approved entry.

```bash
meal-cli apply-text-mappings
# apply-text-mappings done:
#   approved queue rows : 87
#   rows updated        : 142
```

## Servers

### `meal-cli serve-web`

Start the FastAPI read-mostly UI.

```bash
meal-cli serve-web                                # 127.0.0.1:8000
meal-cli serve-web --host 0.0.0.0 --port 5000
meal-cli serve-web --reload                       # dev auto-reload
```

Bootstrap order:
1. Verify the DB file exists (exit 1 with "Run 'meal-cli db-init' first"
   if not).
2. `run_migrations(conn)` — log any applied migrations.
3. Export `PANTRY_COOKING_VIBES_DB`, hand off to `uvicorn.run`.

### `meal-cli serve-mcp`

Start the MCP server (stdio transport) for Claude Code.

```bash
meal-cli serve-mcp
```

Typically wired up in your `.mcp.json` / Claude Code config as an
external server. See [`docs/mcp.md`](mcp.md).

## Exit codes

| Code | Meaning                                                |
| ---- | ------------------------------------------------------ |
| 0    | Success                                                |
| 1    | Preconditions missing (no DB, no JSONL, queue id absent, invalid source name) or user-facing validation failed |
| 2    | `import-url` network fetch failed; `approve-mapping` with no canonical to approve |
