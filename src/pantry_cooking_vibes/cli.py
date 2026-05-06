from pathlib import Path

import typer

from pantry_cooking_vibes.db import DB_PATH, connect, init_db

app = typer.Typer(help="pantry-cooking-vibes CLI", no_args_is_help=True)


@app.command("db-init")
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


@app.command("normalize-recipes")
def normalize_recipes(
    db: Path = typer.Option(
        DB_PATH,
        envvar="PANTRY_COOKING_VIBES_DB",
        help="Path to SQLite database file (env: PANTRY_COOKING_VIBES_DB)",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress output"),
) -> None:
    """Backfill recipe_ingredients.canonical_id by fuzzy-matching original_text.

    Auto-writes only matches that clear the AUTO_APPROVE_THRESHOLD (≥90).
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


@app.command("apply-text-mappings")
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


@app.command("review-mappings")
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


@app.command("approve-mapping")
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


@app.command("reject-mapping")
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


@app.command("import-url")
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


@app.command("ingest")
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
        )
    except FileNotFoundError as e:
        typer.echo(f"JSONL not found: {e}", err=True)
        raise typer.Exit(1) from None
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"ingest done (source={source}):")
    typer.echo(f"  processed   : {stats['processed']}")
    typer.echo(f"  recipes     : {stats['recipes']}")
    typer.echo(f"  ingredients : {stats['ingredients']}")
    typer.echo(f"  tags        : {stats['tags']}")
    typer.echo(f"  skipped     : {stats['skipped']}")


@app.command("list-sources")
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


@app.command("serve-mcp")
def serve_mcp() -> None:
    """Start the MCP server (stdio transport) for Claude Code."""
    from pantry_cooking_vibes.mcp_server.server import main

    main()


@app.command("serve-web")
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
    """Start the FastAPI read-only web UI (pantry is read-write)."""
    import os

    import uvicorn

    db_resolved = Path(db).resolve()
    if not db_resolved.exists():
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


@app.command("db-backup")
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


if __name__ == "__main__":
    app()
