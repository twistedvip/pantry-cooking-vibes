# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-06-15

The first feature release on top of the v0.1.0 baseline. Headline change is
that the web UI can now add recipes itself (previously CLI/MCP only), via a
staging **import inbox**; the rest is recipe-browsing and editing polish.

### Added

- **Import inbox** — the web UI funnels every recipe import through a staging
  area at `/imports`. Paste URLs or upload a JSON-LD file; each item is parsed
  (reusing the CLI's schema.org parser, SSRF guard, and ingredient queue) and
  classified `ready` / `needs review` / `duplicate` / `failed`. Triage by
  status, fix a failed item inline, then save selected items into the library
  or discard them. A batch leaves no history once every item is resolved
  (issue #12).
- Edit recipes from the web UI (issue #49).
- Per-serving nutrition panel on the recipe detail page, rendering the macros
  already stored in `recipes.nutrition_json` (calories, protein, carbs, fat,
  fiber, sodium). Macros with no value are omitted, and recipes without
  nutrition data show no panel (issue #48).
- Pagination on the recipes page (issue #54), plus sorting by ingredient
  match (issue #60).
- `meal-cli delete` for recipes, plans, and pantry items (issue #35).
- Category-scoped unit dropdown in the pantry, replacing the free-text unit
  field (issue #44).
- HelloFresh scraper as a git submodule under `scrapers/` (issue #50).

### Fixed

- URL import failed on sites that serve Brotli-compressed responses (e.g.
  budgetbytes.com): we advertised `br` in `Accept-Encoding` without a Brotli
  decoder installed, so the response body was never decoded and no recipe was
  found. We now only advertise the encodings we can actually decode
  (issues #66, #67).

### Changed

- Recipe-browsing UI polish and improved installation/architecture docs
  (issues #46, #56, #58).
- Dependency bumps: `starlette` 1.0.0 → 1.0.1, `idna` 3.11 → 3.15.

## [0.1.0] — 2026-05-15

First tagged release. Establishes a clean baseline schema and a published
container image so downstream deployments (Portainer / `docker compose`)
can pin a version instead of rebuilding from `main`.

### Added

- Multi-arch Docker image published to
  `ghcr.io/twistedvip/pantry-cooking-vibes` on tagged releases.
- `freshness_days` column on `canonical_ingredients`, seeded from
  `canonical_seed.csv` (drives the pantry expiry-date suggestion).
- `recipe_favorites` + `meal_plan_favorites` tables in the baseline
  schema.
- Partial unique index `idx_meal_plans_week_draft` (at most one draft
  plan per `week_of`).
- `CHANGELOG.md` (this file).

### Changed

- Collapsed the pre-tag migration sequence (001–006) into the v0.1.0
  baseline `_assets/schema.sql`. The migrations directory is now empty
  but the `run_migrations()` machinery and `schema_migrations`
  bookkeeping stay in place for v0.2+ additive changes.
- `recipes.source` is now free-form text with no CHECK constraint;
  validation lives at the application layer
  (`^[a-z][a-z0-9-]*$`).
- `serve-web` bootstrap still calls `run_migrations` on startup so older
  deployed DBs self-heal when a future release adds migrations.

### Removed

- `src/pantry_cooking_vibes/_assets/migrations/001_recipe_favorites.sql`
- `src/pantry_cooking_vibes/_assets/migrations/002_recipes_source_hellofresh.sql`
- `src/pantry_cooking_vibes/_assets/migrations/003_recipes_source_freeform.sql`
- `src/pantry_cooking_vibes/_assets/migrations/004_drop_recipes_without_image.sql`
- `src/pantry_cooking_vibes/_assets/migrations/005_meal_plan_favorites.sql`
- `src/pantry_cooking_vibes/_assets/migrations/006_canonical_freshness_days.sql`

[Unreleased]: https://github.com/twistedvip/pantry-cooking-vibes/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/twistedvip/pantry-cooking-vibes/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/twistedvip/pantry-cooking-vibes/releases/tag/v0.1.0
