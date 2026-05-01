# JSONL Ingest Contract

Scrapers feed the `pantry-cooking-vibes` core via newline-delimited JSON.
One JSON object per line, one recipe per object. The core ingest
(`meal-cli ingest`) validates each line against the `RecipeRecord` Pydantic
model and UPSERTs into `recipes` / `recipe_tags` / `recipe_ingredients`
keyed on `(source, source_id)`.

This file is the source of truth for that wire format. If you maintain a
scraper that targets `meal-cli ingest`, conform to this spec.

---

## Encoding & framing

- **Encoding:** UTF-8.
- **Framing:** one JSON object per line. Trailing newline at end of file.
  Blank lines are ignored. Lines that fail JSON parsing are silently
  skipped (no aborting the run).
- **Schema version:** include `"schema_version": 1` if you want to be
  explicit; absent is treated as `1`. Future bumps will be migrations,
  not breaking changes within v1.

---

## Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | int | no | Defaults to `1`. |
| `source_id` | string | **yes** | Unique per source. Drives the UPSERT key `(source, source_id)`. Use the site's stable identifier (slug, numeric id, canonical URL). Re-importing the same `source_id` overwrites the row. |
| `name` | string | **yes** | Recipe title. Non-empty after `strip()`. |
| `servings` | int | no | Yield as integer count. Use the highest plausible value if the source gives a range ("4–6 servings" → `6`). Omit if unknown. |
| `cooking_time_min` | int | no | Whole minutes (active + passive). Convert ISO-8601 durations like `PT1H30M` to `90`. |
| `instructions_md` | string | no | See [Instructions](#instructions) below. |
| `image_url` | string | no | Absolute https URL preferred. |
| `rating` | float | no | 0.0–5.0 inclusive. Validation rejects out-of-range. |
| `rating_count` | int | no | Number of reviews backing `rating`. |
| `nutrition_json` | object | no | See [Nutrition](#nutrition) below. |
| `tags` | array of string | no | See [Tags](#tags) below. Defaults to `[]`. |
| `ingredients` | array of object | **yes** | See [Ingredients](#ingredients) below. May be `[]` but the key must be present. |

`source` is **not** in the JSONL — it's supplied at ingest time
(`meal-cli ingest <file> --source NAME`). Validated as
`^[a-z][a-z0-9-]*$`.

---

## Instructions

`instructions_md` is an optional Markdown string with a deliberately small
subset.

- **Encoding:** UTF-8. Normalize CRLF → LF. Strip leading/trailing whitespace.
- **Allowed:** paragraphs (blank-line separated), ordered lists (`1. `,
  `2. ` …), unordered lists (`- ` or `* `), headings `h1`–`h3`, bold
  (`**…**`), italic (`*…*`), inline links (`[text](url)`).
- **Disallowed:** raw HTML tags (run an HTML→text pass first), images,
  tables, scripts, embeds.
- **Step structure:** prefer a numbered list when the source has clear
  sequential steps. Free-form prose is acceptable when the source has
  none.
- **Don't pack into instructions:** servings, prep/cook time, ingredient
  list. Those have their own fields — keeping them separate keeps
  querying clean.
- **Soft cap:** ~50 KB. Ingest doesn't enforce, but huge bodies bloat
  the FTS5 index.

Example:

```markdown
1. Heat oil in a wok over high heat.
2. Add garlic and ginger; stir 30 seconds.
3. Push aromatics aside; crack in eggs and scramble.
4. Add rice and soy sauce; toss until coated.

Garnish with scallions and serve.
```

---

## Nutrition

`nutrition_json` is an object (not a JSON string) keyed by canonical
nutrient names. Suggested keys, units, and types:

| Key | Unit | Type |
|---|---|---|
| `calories` | kcal | int |
| `protein_g` | grams | int |
| `carbs_g` | grams | int |
| `fat_g` | grams | int |
| `saturated_fat_g` | grams | int |
| `fiber_g` | grams | int |
| `sugar_g` | grams | int |
| `sodium_mg` | mg | int |

Extra keys are allowed but won't be surfaced in the UI. Omit the field
entirely if the source has no nutrition data — don't emit `null`.

The DB stores nutrition as a JSON string; the ingest does the
serialization. Don't pre-stringify on the wire.

Example:

```json
"nutrition_json": {"calories": 420, "protein_g": 18, "carbs_g": 55, "fat_g": 14}
```

---

## Tags

`tags` is an array of short slug-style strings.

- Lowercased on ingest. Whitespace stripped.
- Deduplicated on ingest (case-insensitive). You don't need to dedupe
  upstream.
- Recommended style: `vegetarian`, `gluten-free`, `quick`, `one-pan`,
  `weeknight`. Avoid sentence-fragment tags like `"easy weeknight dinner"`.

Example:

```json
"tags": ["vegetarian", "quick", "asian"]
```

---

## Ingredients

`ingredients` is an array of objects. Each object describes one
ingredient line.

| Field | Type | Required | Notes |
|---|---|---|---|
| `original_text` | string | recommended | The verbatim line as the source published it (e.g. `"1 cup all-purpose flour"`). Stored as-is for display + fuzzy match against the canonical dictionary. |
| `quantity` | float | no | Decimal. Convert fractions: `0.5` not `"½"` or `"1/2"`. |
| `unit` | string | no | Free-form short string (`cup`, `tbsp`, `oz`, `g`). Core does not normalize units in v1. |
| `notes` | string | no | Free-form modifier (`"chopped"`, `"to taste"`). |
| `canonical_hint` | string | no | Soft suggestion for the canonical-ingredient mapping queue. Used as the lookup key (after `lower()` + `strip()`) before falling back to `original_text`. |

At least `original_text` should be set; otherwise the ingredient lands
with no display text and no mapping-queue key.

The `canonical_id` is **not** in the JSONL. Core resolves it via
`ingredient_mapping_queue` after ingest:

1. Ingest enqueues every distinct `canonical_hint` (or `original_text`)
   under the supplied `--source`.
2. Curator reviews via `meal-cli review-mappings` /
   `approve-mapping` / `reject-mapping`, or the web UI.
3. Approved rows back-fill `recipe_ingredients.canonical_id` on the
   next ingest.

Unmapped ingredients show as "uncategorized" in shopping lists. This is
expected — there's no requirement to pre-map.

Example:

```json
"ingredients": [
  {"original_text": "2 cups jasmine rice, cooked", "quantity": 2.0, "unit": "cup", "canonical_hint": "rice"},
  {"original_text": "3 tbsp soy sauce", "quantity": 3.0, "unit": "tbsp", "canonical_hint": "soy sauce"},
  {"original_text": "salt to taste", "notes": "to taste"}
]
```

---

## Idempotency

Re-running ingest on the same JSONL is safe.

- Recipes UPSERT on `(source, source_id)` — re-imports overwrite the
  metadata in place.
- `recipe_tags` and `recipe_ingredients` are **deleted and re-inserted
  wholesale** per recipe. If you remove a tag upstream, it disappears
  on next ingest. There is no "drift" — the JSONL is the authority.
- Mapping queue rows persist across ingests. Approved mappings keep
  their `canonical_id` link.

---

## Plugin escape hatch

If your scraper needs site-specific cleanup before validation
(editorial-marker stripping, brand-suffix tweaks, unit canonicalisation,
filtering stub records), ship a Python class implementing the
`RecipeImporter` protocol and register it under entry-point group
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

Invoke with `--plugin`:

```bash
meal-cli ingest example.jsonl --source example --plugin example
```

Without `--plugin`, the plain JSONL contract is used.

---

## Validation behaviour

- Lines that fail `RecipeRecord` validation are counted as `skipped` in
  the ingest stats; the run continues. Inspect the returned counts:

  ```text
  ingest done (source=mysource):
    processed   : 1234
    recipes     : 1230
    ingredients : 8721
    tags        : 4112
    skipped     : 4
  ```

- Required fields (`source_id`, `name`, `ingredients`) missing → skip.
- `rating` outside `[0, 5]` → skip.
- `source_id` or `name` empty after `strip()` → skip.

If `skipped > 0` and you want to know why, run with a Python debugger
or temporarily wrap the validation in `try/except ValidationError as e:
print(e.json())` to surface the field-level errors.

---

## Minimal valid example

```jsonl
{"source_id":"chicken-fried-rice","name":"Chicken Fried Rice","ingredients":[{"original_text":"2 cups cooked rice"},{"original_text":"1 egg"}]}
```

That's the floor. Everything else is optional.
