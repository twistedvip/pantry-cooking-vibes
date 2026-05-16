from pathlib import Path

import typer

from pantry_cooking_vibes.db import DB_PATH, connect, init_db

app = typer.Typer(help="pantry-cooking-vibes CLI", no_args_is_help=True)

# Help-panel labels grouped most-common -> least-common in --help output.
# Typer orders panels by first command appearance, so command definitions are
# kept in the same order as the desired panel order below.
_PANEL_RUN = "Run"
_PANEL_SETUP = "Setup"
_PANEL_RECIPES = "Recipes"
_PANEL_CURATION = "Curation"


def _run_web(host: str, port: int, reload: bool, db: Path, auto_init: bool) -> None:
    """Shared serve-web bootstrap. ``auto_init=True`` initializes a missing DB
    in place (used by ``start``); ``False`` exits 1 with a hint (used by
    ``serve-web``)."""
    import os

    import uvicorn

    db_resolved = Path(db).resolve()
    if not db_resolved.exists():
        if auto_init:
            typer.echo(f"No database found at {db_resolved} — initializing...")
            seeded = init_db(db_path=db_resolved)
            typer.echo(f"Database initialized: {db_resolved}")
            typer.echo(f"Canonical ingredients seeded: {seeded}")
        else:
            typer.echo(f"Database not found: {db_resolved}", err=True)
            typer.echo("Run 'meal-cli db-init' first.", err=True)
            raise typer.Exit(1)

    # Apply any migrations the DB is behind on, so a stale file doesn't
    # surface as a 500 at query time (e.g. missing recipe_favorites table).
    from pantry_cooking_vibes.db import run_migrations

    with connect(db_resolved) as conn:
        ran = run_migrations(conn)
    if ran:
        typer.echo(f"Applied pending migrations: {', '.join(ran)}")

    # Pin the db path for the app factory. Env var survives uvicorn's reloader.
    os.environ["PANTRY_COOKING_VIBES_DB"] = str(db_resolved)
    typer.echo(f"Starting web UI at http://{host}:{port} (db={db_resolved})")
    uvicorn.run(
        "pantry_cooking_vibes.web.app_factory:app",
        host=host,
        port=port,
        reload=reload,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@app.command("start", rich_help_panel=_PANEL_RUN)
def start(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev)"),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """Start the web UI. Initializes the database if it doesn't exist yet.

    Recommended entry point for new installs. For scripted deployments that
    require an explicit ``db-init`` step, use ``serve-web`` instead.
    """
    _run_web(host=host, port=port, reload=reload, db=db, auto_init=True)


@app.command("serve-web", rich_help_panel=_PANEL_RUN)
def serve_web(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev)"),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """Start the FastAPI web UI. Exits 1 if the database is missing.

    Scripted/CI-friendly form of ``start`` — assumes the DB has already been
    initialized. For first-run flows, use ``start`` instead (auto-inits).
    """
    _run_web(host=host, port=port, reload=reload, db=db, auto_init=False)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@app.command("db-init", rich_help_panel=_PANEL_SETUP)
def db_init(
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """Initialize the database: apply schema, run migrations, seed canonical ingredients."""
    seeded = init_db(db_path=db)
    typer.echo(f"Database initialized: {db}")
    typer.echo(f"Canonical ingredients seeded: {seeded}")
    with connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM canonical_ingredients").fetchone()[0]
    typer.echo(f"Total canonical_ingredients rows: {total}")


@app.command("db-backup", rich_help_panel=_PANEL_SETUP)
def db_backup(
    dest: Path = typer.Argument(
        ...,
        help="Destination file, or a directory (in which case a timestamped "
        "filename is generated inside it).",
    ),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """Back up the SQLite database using the online backup API."""
    import os
    import sqlite3 as _sqlite3
    from datetime import datetime

    db_resolved = db.resolve()
    if not db_resolved.exists():
        typer.echo(f"Database not found: {db_resolved}", err=True)
        typer.echo("Run 'meal-cli db-init' first.", err=True)
        raise typer.Exit(1)

    # Treat a trailing separator or an existing directory as "write into this
    # directory with a timestamped filename". sqlite3.connect() on a directory
    # path raises the cryptic "unable to open database file" error, so we
    # disambiguate here before handing the path to sqlite.
    raw = str(dest)
    looks_like_dir = dest.is_dir() or raw.endswith(("/", "\\", os.sep))
    if looks_like_dir:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest / f"{db_resolved.stem}-{stamp}.db"

    dest_resolved = dest.resolve()
    dest_resolved.parent.mkdir(parents=True, exist_ok=True)
    if dest_resolved.is_dir():
        typer.echo(
            f"Destination is a directory: {dest_resolved}. "
            "Pass a file path or a directory with a trailing separator.",
            err=True,
        )
        raise typer.Exit(1)

    src = _sqlite3.connect(str(db_resolved))
    try:
        dst = _sqlite3.connect(str(dest_resolved))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    typer.echo(f"Backed up {db_resolved} -> {dest_resolved}")


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


@app.command("ingest", rich_help_panel=_PANEL_RECIPES)
def ingest_cmd(
    jsonl_path: Path = typer.Argument(..., help="Path to JSONL file produced by a scraper"),
    source: str = typer.Option(
        None,
        "--source",
        help="Source name (lowercase letters/digits/hyphens). Prompted if omitted.",
    ),
    plugin: str = typer.Option(
        None,
        "--plugin",
        help="Optional plugin name (entry-point group 'pantry_cooking_vibes.importers') "
        "to post-process records before validation.",
    ),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress output"),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help=(
            "Emit phase-by-phase progress to stderr (load, validation, dedup, "
            "write batches with rate/ETA)."
        ),
    ),
    dedup: bool = typer.Option(
        True,
        "--dedup/--no-dedup",
        help=(
            "Detect and skip same-source duplicate recipes (e.g. HungryRoot "
            "serving-size variants). Best variant by rating_count > rating > "
            "instructions length wins. Use --no-dedup to import every record."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Run validation + dedup but write nothing. Stats and skipped-"
            "duplicate log show what a real ingest would do."
        ),
    ),
    normalize: bool = typer.Option(
        True,
        "--normalize/--no-normalize",
        help=(
            "After a successful ingest, fuzzy-match new recipe ingredients "
            "against canonical_ingredients (same as `normalize-recipes`). "
            "Use --no-normalize to skip. Skipped on --dry-run or when no "
            "recipes were written."
        ),
    ),
) -> None:
    """Ingest a JSONL file conforming to docs/jsonl_contract.md."""
    from pantry_cooking_vibes.importers.jsonl_ingest import ingest_jsonl
    from pantry_cooking_vibes.mcp_server.tools import list_recipe_sources
    from pantry_cooking_vibes.models import SOURCE_NAME_RE

    if not jsonl_path.exists():
        typer.echo(f"JSONL not found: {jsonl_path}", err=True)
        raise typer.Exit(1)

    if source is None:
        existing = list_recipe_sources(db_path=db)
        if existing:
            typer.echo("Existing sources: " + ", ".join(existing))
        else:
            typer.echo("No existing sources yet.")
        source = typer.prompt(
            "Source name (lowercase letters/digits/hyphens, e.g. 'manual')"
        ).strip()

    if not SOURCE_NAME_RE.fullmatch(source):
        typer.echo(
            f"invalid source name {source!r}: must be lowercase letters, "
            "digits, or hyphens, starting with a letter",
            err=True,
        )
        raise typer.Exit(1)

    try:
        stats = ingest_jsonl(
            jsonl_path,
            source,
            db_path=db,
            plugin=plugin,
            quiet=quiet,
            dedup=dedup,
            dry_run=dry_run,
            verbose=verbose,
        )
    except FileNotFoundError as e:
        typer.echo(f"JSONL not found: {e}", err=True)
        raise typer.Exit(1) from None
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None

    label = "dry-run" if dry_run else "ingest done"
    typer.echo(f"{label} (source={source}):")
    typer.echo(f"  processed          : {stats['processed']}")
    typer.echo(f"  recipes            : {stats['recipes']}")
    typer.echo(f"  ingredients        : {stats['ingredients']}")
    typer.echo(f"  tags               : {stats['tags']}")
    typer.echo(f"  skipped (invalid)  : {stats['skipped']}")
    typer.echo(f"  duplicates_skipped : {stats['duplicates_skipped']}")

    if not normalize:
        return
    if dry_run:
        typer.echo("normalize: skipped (dry-run)")
        return
    if stats["recipes"] == 0:
        typer.echo("normalize: skipped (no recipes written)")
        return

    from pantry_cooking_vibes.importers.normalize import backfill_recipe_canonicals

    norm = backfill_recipe_canonicals(db_path=db, quiet=quiet)
    typer.echo("normalize done:")
    typer.echo(f"  distinct texts : {norm['distinct_texts']}")
    typer.echo(f"  auto-approved  : {norm['approved']}")
    typer.echo(f"  proposed       : {norm['proposed']}")
    typer.echo(f"  no_match       : {norm['no_match']}")
    typer.echo(f"  rows updated   : {norm['rows_updated']}")


@app.command("import-url", rich_help_panel=_PANEL_RECIPES)
def import_url_cmd(
    url: str = typer.Argument(..., help="Recipe URL"),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress output"),
) -> None:
    """Import a recipe from a URL via schema.org JSON-LD."""
    from pantry_cooking_vibes.importers.url_import import (
        RecipeMissingImageError,
        RecipeNotFoundError,
        import_url,
    )

    try:
        stats = import_url(url, db_path=db, quiet=quiet)
    except (RecipeNotFoundError, RecipeMissingImageError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    except Exception as e:
        typer.echo(f"fetch failed: {e}", err=True)
        raise typer.Exit(2) from e

    typer.echo(f"import-url done: recipe id={stats['recipe_id']} '{stats['name']}'")
    typer.echo(f"  ingredients : {stats['ingredients']}")
    typer.echo(f"  tags        : {stats['tags']}")


@app.command("list-sources", rich_help_panel=_PANEL_RECIPES)
def list_sources_cmd(
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """List distinct recipe sources currently in the database with recipe counts."""
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM recipes GROUP BY source ORDER BY source"
        ).fetchall()

    if not rows:
        typer.echo("No recipes in database.")
        return

    width = max(len(r["source"]) for r in rows)
    typer.echo(f"{'SOURCE':<{width}}  RECIPES")
    typer.echo("-" * (width + 9))
    for r in rows:
        typer.echo(f"{r['source']:<{width}}  {r['n']}")


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------


@app.command("normalize-recipes", rich_help_panel=_PANEL_CURATION)
def normalize_recipes(
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress output"),
) -> None:
    """Backfill recipe_ingredients.canonical_id by fuzzy-matching original_text.

    Auto-writes only matches that clear the AUTO_APPROVE_THRESHOLD (≥85).
    Skips rows that already have a canonical_id.
    """
    from pantry_cooking_vibes.importers.normalize import backfill_recipe_canonicals

    stats = backfill_recipe_canonicals(db_path=db, quiet=quiet)
    typer.echo("normalize-recipes done:")
    typer.echo(f"  distinct texts : {stats['distinct_texts']}")
    typer.echo(f"  auto-approved  : {stats['approved']}")
    typer.echo(f"  proposed       : {stats['proposed']}")
    typer.echo(f"  no_match       : {stats['no_match']}")
    typer.echo(f"  rows updated   : {stats['rows_updated']}")


@app.command("review-mappings", rich_help_panel=_PANEL_CURATION)
def review_mappings(
    limit: int = typer.Option(50, help="Max rows to show"),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """List pending canonical-ingredient proposals for human review."""
    from pantry_cooking_vibes.importers.normalize import review_pending

    rows = review_pending(db_path=db, limit=limit)
    if not rows:
        typer.echo("No pending mappings — all products are approved or rejected.")
        return

    typer.echo(f"{'ID':>5}  {'STATUS':<10}  {'CONF':>5}  {'ORIGINAL':<35}  PROPOSED CANONICAL")
    typer.echo("-" * 85)
    for r in rows:
        canonical = r["canonical_name"] or "(none)"
        typer.echo(
            f"{r['id']:>5}  {r['status']:<10}  {r['confidence']:>5.2f}"
            f"  {r['original_text'][:35]:<35}  {canonical}"
        )
    typer.echo(f"\n{len(rows)} row(s) shown (limit={limit}).")


@app.command("approve-mapping", rich_help_panel=_PANEL_CURATION)
def approve_mapping_cmd(
    queue_id: int = typer.Argument(..., help="ingredient_mapping_queue.id to approve"),
    canonical_id: int = typer.Option(
        None,
        help="Override the proposed canonical_id. Default: keep current proposal.",
    ),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """Approve a queued ingredient mapping (optionally overriding the canonical)."""
    from pantry_cooking_vibes.importers.normalize import approve_mapping

    with connect(db) as conn:
        row = conn.execute(
            "SELECT proposed_canonical_id, status FROM ingredient_mapping_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    if row is None:
        typer.echo(f"queue id {queue_id} not found", err=True)
        raise typer.Exit(1)

    target = canonical_id if canonical_id is not None else row["proposed_canonical_id"]
    if target is None:
        typer.echo(
            f"queue id {queue_id} has no proposed canonical; pass --canonical-id explicitly",
            err=True,
        )
        raise typer.Exit(2)

    approve_mapping(queue_id, target, db_path=db)
    typer.echo(f"approved queue id {queue_id} -> canonical {target}")


@app.command("reject-mapping", rich_help_panel=_PANEL_CURATION)
def reject_mapping_cmd(
    queue_id: int = typer.Argument(..., help="ingredient_mapping_queue.id to reject"),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """Reject a queued ingredient mapping."""
    from pantry_cooking_vibes.importers.normalize import reject_mapping

    with connect(db) as conn:
        exists = conn.execute(
            "SELECT 1 FROM ingredient_mapping_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    if exists is None:
        typer.echo(f"queue id {queue_id} not found", err=True)
        raise typer.Exit(1)

    reject_mapping(queue_id, db_path=db)
    typer.echo(f"rejected queue id {queue_id}")


_DELETE_TYPES = ("recipe", "plan", "pantry")


def _describe_delete_target(item_type: str, item_id: int, db: Path) -> str | None:
    """Return a human-readable label for the row, or None if not found."""
    with connect(db) as conn:
        if item_type == "recipe":
            row = conn.execute("SELECT name FROM recipes WHERE id = ?", (item_id,)).fetchone()
            return f"recipe {item_id} ({row['name']!r})" if row else None
        if item_type == "plan":
            row = conn.execute("SELECT week_of FROM meal_plans WHERE id = ?", (item_id,)).fetchone()
            return f"meal plan {item_id} (week_of {row['week_of']})" if row else None
        # pantry
        row = conn.execute(
            "SELECT ci.name FROM pantry p "
            "JOIN canonical_ingredients ci ON ci.id = p.canonical_id "
            "WHERE p.id = ?",
            (item_id,),
        ).fetchone()
        return f"pantry item {item_id} ({row['name']})" if row else None


def _delete_one(item_type: str, item_id: int, db: Path, yes: bool) -> None:
    from pantry_cooking_vibes.mcp_server import tools

    label = _describe_delete_target(item_type, item_id, db)
    if label is None:
        typer.echo(f"{item_type} {item_id} not found", err=True)
        raise typer.Exit(1)

    if not yes and not typer.confirm(f"Delete {label}?", default=False):
        typer.echo("aborted")
        raise typer.Exit(0)

    if item_type == "recipe":
        tools.delete_recipe(item_id, db_path=db)
    elif item_type == "plan":
        tools.delete_meal_plan(item_id, db_path=db)
    else:  # pantry
        tools.remove_pantry_item(item_id, db_path=db)
    typer.echo(f"deleted {label}")


def _delete_all(item_type: str, db: Path, yes: bool) -> None:
    table = {"recipe": "recipes", "plan": "meal_plans", "pantry": "pantry"}[item_type]
    with connect(db) as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608

    if count == 0:
        typer.echo(f"no {item_type} rows to delete")
        return

    if not yes and not typer.confirm(
        f"Delete ALL {count} {item_type} row(s)? This cannot be undone.",
        default=False,
    ):
        typer.echo("aborted")
        raise typer.Exit(0)

    with connect(db) as conn:
        if item_type == "recipe":
            # Row-level deletes keep recipes_fts in sync via the recipes_ad trigger.
            ids = [r["id"] for r in conn.execute("SELECT id FROM recipes")]
            for rid in ids:
                conn.execute("DELETE FROM recipes WHERE id = ?", (rid,))
        else:
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
    typer.echo(f"deleted {count} {item_type} row(s)")


@app.command("delete", rich_help_panel=_PANEL_CURATION)
def delete_cmd(
    item_type: str = typer.Argument(
        ...,
        metavar="TYPE",
        help=f"Item type. One of: {', '.join(_DELETE_TYPES)}.",
    ),
    id: int = typer.Option(
        None,
        "--id",
        help="Specific item id. Omit to delete ALL items of the given type.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
) -> None:
    """Delete a recipe, meal plan, or pantry item.

    With ``--id`` deletes that single row. Without ``--id`` deletes ALL rows
    of the given type (count-aware confirmation). Use ``--yes`` to skip
    prompts in scripts. Cascades follow ON DELETE CASCADE foreign keys.
    """
    if item_type not in _DELETE_TYPES:
        typer.echo(
            f"invalid type {item_type!r}; choose: {', '.join(_DELETE_TYPES)}",
            err=True,
        )
        raise typer.Exit(1)

    if id is not None:
        _delete_one(item_type, id, db, yes)
    else:
        _delete_all(item_type, db, yes)


@app.command("apply-text-mappings", rich_help_panel=_PANEL_CURATION)
def apply_text_mappings_cmd(
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress output"),
) -> None:
    """Apply approved 'recipe_ingredient_text' queue rows to recipe_ingredients."""
    from pantry_cooking_vibes.importers.normalize import apply_text_mappings

    stats = apply_text_mappings(db_path=db, quiet=quiet)
    typer.echo("apply-text-mappings done:")
    typer.echo(f"  approved queue rows : {stats['approved_queue_rows']}")
    typer.echo(f"  rows updated        : {stats['rows_updated']}")


# ---------------------------------------------------------------------------
# Hidden (experimental)
# ---------------------------------------------------------------------------


@app.command("serve-mcp", hidden=True)
def serve_mcp() -> None:
    """Start the MCP server (stdio transport) for Claude Code.

    Experimental — hidden from --help until the MCP surface is fully tested.
    Still callable explicitly: ``meal-cli serve-mcp``.
    """
    from pantry_cooking_vibes.mcp_server.server import main

    main()


if __name__ == "__main__":
    app()
