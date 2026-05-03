#!/bin/sh
# First-run container init for pantry-cooking-vibes.
#
# If $PANTRY_COOKING_VIBES_DB (default /app/data/store/app.db) is missing on
# container start, run `meal-cli db-init` to apply schema + seed canonical
# ingredients, then ingest data/seed/demo.jsonl so the UI isn't empty on
# first visit.
#
# On subsequent restarts the file exists, so we no-op and exec straight into
# the user's command.
#
# meal-cli reads $PANTRY_COOKING_VIBES_DB via Typer's envvar= binding, so we
# don't need to pass --db explicitly.
#
# Idempotent by design: db-init itself uses CREATE TABLE IF NOT EXISTS, so
# even if our existence check ever races we won't corrupt anything.

set -eu

DB="${PANTRY_COOKING_VIBES_DB:-/app/data/store/app.db}"
SEED_JSONL="/app/data/seed/demo.jsonl"

# Ensure the persistent dir exists (volume mounts may not pre-create it).
mkdir -p "$(dirname "$DB")"

if [ ! -f "$DB" ]; then
    echo "[entrypoint] $DB missing — running first-run init" >&2
    meal-cli db-init

    if [ -f "$SEED_JSONL" ]; then
        echo "[entrypoint] ingesting demo recipe from $SEED_JSONL" >&2
        meal-cli ingest "$SEED_JSONL" --source manual || \
            echo "[entrypoint] demo ingest failed (non-fatal)" >&2
    fi
else
    echo "[entrypoint] $DB present — skipping init" >&2
fi

exec "$@"
