# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/twistedvip/pantry-cooking-vibes/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/twistedvip/pantry-cooking-vibes/releases/tag/v0.1.0
