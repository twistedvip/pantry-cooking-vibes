# Importers

The core ships **two** ingestion paths. Anything more site-specific lives
in a separate scraper repo and reaches the core via the JSONL contract.

1. **JSONL ingest** (`importers/jsonl_ingest.py`) — bulk path. Validates
   each line of a scraper-produced JSONL against the documented contract
   and UPSERTs.
2. **URL import** (`importers/url_import.py`) — one-off path. Fetches a
   single page, extracts schema.org Recipe JSON-LD, and UPSERTs.

Both UPSERT on `(source, source_id)` and replace tags/ingredients
wholesale, so re-running is a safe refresh.

The wire format for ingest is documented separately in
[`jsonl_contract.md`](jsonl_contract.md). This page covers the
*implementation* of each path and the plugin escape hatch.

## JSONL ingest (`importers/jsonl_ingest.py`)

```
meal-cli ingest <file> --source <name> [--plugin <name>]
   │
   v
ingest_jsonl(jsonl_path, source, *, db_path, plugin, quiet)
   │
   ├─ validate source name (^[a-z][a-z0-9-]*$)
   ├─ read JSONL line-by-line, parse JSON
   ├─ if plugin: registry.load_plugin(name).post_process(records)
   ├─ for each record:
   │     RecipeRecord.model_validate(record)        ← Pydantic
   │     UPSERT into recipes (source, source_id, ...)
   │     DELETE recipe_ingredients / recipe_tags WHERE recipe_id=?
   │     INSERT recipe_ingredients / recipe_tags rows
   │     enqueue distinct canonical_hint/original_text into
   │     ingredient_mapping_queue
   └─ return IngestStats(processed, recipes, ingredients, tags, skipped)
```

Validation failures, JSON parse errors, and missing required fields are
counted as `skipped` and the run continues. Empty lines are ignored.

### IngestStats shape

```python
class IngestStats(TypedDict):
    processed: int    # JSON-parseable lines (excludes blank/garbage)
    recipes: int      # RecipeRecord rows successfully UPSERTed
    ingredients: int  # recipe_ingredients rows inserted
    tags: int         # recipe_tags rows inserted
    skipped: int      # validation failures + missing required fields
```

## URL import (`importers/url_import.py`)

```
meal-cli import-url <url>
   │
   v
fetch_html(url)   ← Chrome-UA'd requests with retry
   │
   v
extract_recipe_jsonld(html)
   │  scans <script type="application/ld+json">, walks @graph
   │  returns first entity whose @type contains "Recipe"
   v
parse_recipe(entity, url)
   │  coerces ISO8601 durations → minutes
   │  handles recipeYield as int | str | list
   │  flattens recipeInstructions (string / HowToStep / HowToSection / list)
   │  collects tags from keywords + recipeCategory + recipeCuisine
   v
UPSERT into recipes (source='url', source_id=<canonical url>)
   │  DELETE + re-INSERT recipe_ingredients / recipe_tags
   v
maps canonical_id from previously-approved ingredient_mapping_queue rows
keyed by lowercased original_text; unresolved rows store canonical_id=NULL
```

`html=` kwarg short-circuits the network fetch — the test suite uses it
to import from saved fixtures.

User-Agent override: set `PANTRY_COOKING_VIBES_UA=<string>` if a site
403s the default fingerprint.

## Ingredient normalization (`importers/normalize.py`)

Bridge between raw ingredient text (from any source) and our
`canonical_ingredients` table. Uses `rapidfuzz.fuzz.WRatio`.

Three callable surfaces:

| Function                       | Used by                          |
| ------------------------------ | -------------------------------- |
| `backfill_recipe_canonicals`   | `meal-cli normalize-recipes`     |
| `apply_text_mappings`          | `meal-cli apply-text-mappings`   |
| `review_pending` / `approve_mapping` / `reject_mapping` | the curator CLIs |
| `run_normalization(source, items)` | plugin scripts that want to pre-populate the queue from a source-specific catalog |

```
recipe_ingredients.original_text
   │
   v
backfill_recipe_canonicals(db_path, quiet)
   │
   ├─ _load_index(conn): canonical_ingredients → choice list (name + aliases)
   ├─ _clean(text): strip brand/packaging/unit noise
   ├─ process.extractOne(cleaned, choices, scorer=WRatio)
   │
   ├─ if score >= AUTO_APPROVE_THRESHOLD (85):
   │     write canonical_id directly to recipe_ingredients,
   │     also queue as 'approved' for audit trail
   │
   └─ else upsert into ingredient_mapping_queue:
            70-89 → status='proposed'
            <70  → status='no_match' (confidence=0)
```

Below-threshold rows are stored with confidence `0.0` so reviewers can
still see what was scanned. Alias collisions (two canonicals sharing an
alias) log a warning and keep the first (lowest `id`) mapping; check
logs if a "wrong" mapping proposal surprises you.

The mapping-queue `source` column is free-form. The default for ingest
is `recipe_ingredient_text`; plugin scripts may use any value (slug, sku,
etc.) when they pre-populate the queue from a source-specific product
catalog.

## Plugin escape hatch (`importers/registry.py`)

Most scrapers should be able to emit clean JSONL directly. When that
isn't enough — editorial-marker stripping, unit canonicalisation, or
filtering stub records — ship a Python class on entry-point group
`pantry_cooking_vibes.importers`:

```toml
# pyproject.toml of your scraper package
[project.entry-points."pantry_cooking_vibes.importers"]
example = "my_scraper.plugin:ExampleImporter"
```

The class:

```python
class ExampleImporter:
    name = "example"
    version = "1.0.0"

    def post_process(self, records: list[dict]) -> list[dict]:
        # Mutate / filter raw records before Pydantic validation.
        # Must not touch the database.
        return [r for r in records if r.get("name")]
```

Discovery is via `importlib.metadata.entry_points`:

```python
from pantry_cooking_vibes.importers.registry import discover_plugins, load_plugin

discover_plugins()        # {"example": <ExampleImporter>}
load_plugin("example")    # raises with helpful list if missing
```

Invoke from the CLI:

```bash
meal-cli ingest example.jsonl --source example --plugin example
```

Without `--plugin`, the plain JSONL contract is used.

## Idempotence

- `ingest_jsonl` and `import_url` both UPSERT on `(source, source_id)`.
- Tags + ingredients are **deleted and re-inserted** wholesale per
  recipe — re-runs are a full refresh, not an append.
- Mapping queue rows persist; approved canonical_id links survive.
- `backfill_recipe_canonicals` skips rows that already have a
  `canonical_id`, so it's safe to run repeatedly.
