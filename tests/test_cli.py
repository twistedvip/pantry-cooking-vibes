"""Tests for meal-cli command bodies (CLI surface)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pantry_cooking_vibes.cli import app
from pantry_cooking_vibes.db import connect

runner = CliRunner()


# ---------- db-init ----------


def test_db_init_creates_file_and_seeds(tmp_path: Path):
    db = tmp_path / "new.db"
    res = runner.invoke(app, ["db-init", "--db", str(db)])
    assert res.exit_code == 0, res.output
    assert db.exists()
    assert "Database initialized" in res.output
    assert "Total canonical_ingredients rows:" in res.output
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM canonical_ingredients").fetchone()[0]
        assert n > 0


# ---------- db-backup ----------


def test_db_backup_to_file_path(db_path: Path, tmp_path: Path):
    dest = tmp_path / "backup.db"
    res = runner.invoke(app, ["db-backup", str(dest), "--db", str(db_path)])
    assert res.exit_code == 0, res.output
    assert dest.exists()
    assert "Backed up" in res.output


def test_db_backup_to_directory_generates_timestamped_filename(db_path: Path, tmp_path: Path):
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    res = runner.invoke(app, ["db-backup", str(dest_dir), "--db", str(db_path)])
    assert res.exit_code == 0, res.output
    files = list(dest_dir.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".db"


def test_db_backup_missing_source_db_exits_1(tmp_path: Path):
    missing = tmp_path / "nope.db"
    dest = tmp_path / "out.db"
    res = runner.invoke(app, ["db-backup", str(dest), "--db", str(missing)])
    assert res.exit_code == 1
    assert "Database not found" in res.output


# ---------- list-sources ----------


def test_list_sources_empty_db(db_path: Path):
    res = runner.invoke(app, ["list-sources", "--db", str(db_path)])
    assert res.exit_code == 0
    assert "No recipes in database." in res.output


def test_list_sources_seeded(seeded_db_path: Path):
    res = runner.invoke(app, ["list-sources", "--db", str(seeded_db_path)])
    assert res.exit_code == 0
    assert "manual" in res.output
    assert "url" in res.output


# ---------- ingest validation paths ----------


def test_ingest_missing_jsonl_exits_1(db_path: Path, tmp_path: Path):
    res = runner.invoke(
        app,
        ["ingest", str(tmp_path / "missing.jsonl"), "--source", "manual", "--db", str(db_path)],
    )
    assert res.exit_code == 1
    assert "JSONL not found" in res.output


def test_ingest_invalid_source_name_exits_1(db_path: Path, tmp_path: Path):
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text("", encoding="utf-8")
    res = runner.invoke(
        app,
        ["ingest", str(jsonl), "--source", "BAD_NAME", "--db", str(db_path)],
    )
    assert res.exit_code == 1
    assert "invalid source name" in res.output


# ---------- normalize-recipes ----------


def test_normalize_recipes_runs_on_seeded(seeded_db_path: Path):
    res = runner.invoke(app, ["normalize-recipes", "--db", str(seeded_db_path), "--quiet"])
    assert res.exit_code == 0, res.output
    assert "normalize-recipes done:" in res.output
    assert "distinct texts" in res.output


# ---------- review-mappings ----------


def test_review_mappings_empty_queue(seeded_db_path: Path):
    res = runner.invoke(app, ["review-mappings", "--db", str(seeded_db_path)])
    assert res.exit_code == 0
    assert "No pending mappings" in res.output


def test_review_mappings_shows_pending(seeded_db_path: Path):
    with connect(seeded_db_path) as conn:
        broccoli_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name = 'broccoli'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO ingredient_mapping_queue "
            "(source, source_key, original_text, proposed_canonical_id, confidence, status) "
            "VALUES ('test', 'sk1', 'broc florets', ?, 0.85, 'proposed')",
            (broccoli_id,),
        )
    res = runner.invoke(app, ["review-mappings", "--db", str(seeded_db_path)])
    assert res.exit_code == 0
    assert "broc florets" in res.output
    assert "row(s) shown" in res.output


# ---------- approve-mapping ----------


def _insert_queue_row(db: Path, canonical_id: int | None = None) -> int:
    with connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO ingredient_mapping_queue "
            "(source, source_key, original_text, proposed_canonical_id, confidence, status) "
            "VALUES ('test', ?, 'broc florets', ?, 0.85, 'proposed') RETURNING id",
            (f"sk-{canonical_id}", canonical_id),
        )
        return cur.fetchone()["id"]


def test_approve_mapping_missing_queue_id_exits_1(seeded_db_path: Path):
    res = runner.invoke(app, ["approve-mapping", "99999", "--db", str(seeded_db_path)])
    assert res.exit_code == 1
    assert "queue id 99999 not found" in res.output


def test_approve_mapping_no_proposed_canonical_exits_2(seeded_db_path: Path):
    qid = _insert_queue_row(seeded_db_path, canonical_id=None)
    res = runner.invoke(app, ["approve-mapping", str(qid), "--db", str(seeded_db_path)])
    assert res.exit_code == 2
    assert "has no proposed canonical" in res.output


def test_approve_mapping_success(seeded_db_path: Path):
    with connect(seeded_db_path) as conn:
        broccoli_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name = 'broccoli'"
        ).fetchone()["id"]
    qid = _insert_queue_row(seeded_db_path, canonical_id=broccoli_id)
    res = runner.invoke(app, ["approve-mapping", str(qid), "--db", str(seeded_db_path)])
    assert res.exit_code == 0, res.output
    assert f"approved queue id {qid}" in res.output


def test_approve_mapping_with_override_canonical(seeded_db_path: Path):
    with connect(seeded_db_path) as conn:
        broccoli_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name = 'broccoli'"
        ).fetchone()["id"]
    qid = _insert_queue_row(seeded_db_path, canonical_id=None)
    res = runner.invoke(
        app,
        [
            "approve-mapping",
            str(qid),
            "--canonical-id",
            str(broccoli_id),
            "--db",
            str(seeded_db_path),
        ],
    )
    assert res.exit_code == 0, res.output


# ---------- reject-mapping ----------


def test_reject_mapping_missing_queue_id_exits_1(seeded_db_path: Path):
    res = runner.invoke(app, ["reject-mapping", "99999", "--db", str(seeded_db_path)])
    assert res.exit_code == 1
    assert "queue id 99999 not found" in res.output


def test_reject_mapping_success(seeded_db_path: Path):
    qid = _insert_queue_row(seeded_db_path, canonical_id=None)
    res = runner.invoke(app, ["reject-mapping", str(qid), "--db", str(seeded_db_path)])
    assert res.exit_code == 0
    assert f"rejected queue id {qid}" in res.output
    with connect(seeded_db_path) as conn:
        row = conn.execute(
            "SELECT status FROM ingredient_mapping_queue WHERE id=?", (qid,)
        ).fetchone()
    assert row["status"] == "rejected"


# ---------- apply-text-mappings ----------


def test_apply_text_mappings_runs(seeded_db_path: Path):
    res = runner.invoke(app, ["apply-text-mappings", "--db", str(seeded_db_path), "--quiet"])
    assert res.exit_code == 0, res.output
    assert "apply-text-mappings done:" in res.output


# ---------- import-url error paths (no network) ----------


def test_import_url_fetch_failure_exits_2(db_path: Path, monkeypatch):
    """Network/parse failures bubble out as exit 2."""
    from pantry_cooking_vibes.importers import url_import

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(url_import, "import_url", boom)
    res = runner.invoke(app, ["import-url", "https://example.com/x", "--db", str(db_path)])
    assert res.exit_code == 2
    assert "fetch failed" in res.output


def test_import_url_recipe_not_found_exits_1(db_path: Path, monkeypatch):
    from pantry_cooking_vibes.importers import url_import

    def boom(*args, **kwargs):
        raise url_import.RecipeNotFoundError("no JSON-LD")

    monkeypatch.setattr(url_import, "import_url", boom)
    res = runner.invoke(app, ["import-url", "https://example.com/x", "--db", str(db_path)])
    assert res.exit_code == 1
    assert "no JSON-LD" in res.output


# ---------- start / serve-web auto-init paths ----------


def test_start_initializes_missing_db(tmp_path: Path, monkeypatch):
    """start should call init_db when the db file is absent (auto_init=True)."""

    db = tmp_path / "fresh.db"
    captured: dict = {}

    def fake_uvicorn_run(target, **kwargs):
        captured["target"] = target
        captured["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    res = runner.invoke(app, ["start", "--db", str(db), "--port", "0"])
    assert res.exit_code == 0, res.output
    assert "No database found" in res.output
    assert "Database initialized" in res.output
    assert db.exists()
    assert captured["target"] == "pantry_cooking_vibes.web.app_factory:app"


def test_serve_web_missing_db_exits_1(tmp_path: Path):
    """serve-web should NOT auto-init; missing DB → exit 1."""
    db = tmp_path / "nope.db"
    res = runner.invoke(app, ["serve-web", "--db", str(db)])
    assert res.exit_code == 1
    assert "Database not found" in res.output


# ---------- serve-mcp (hidden, smoke-only) ----------


def test_serve_mcp_command_exists_but_hidden_from_help():
    """serve-mcp is hidden — shouldn't appear in the top-level --help output."""
    res = runner.invoke(app, ["--help"])
    assert "serve-mcp" not in res.output


def test_serve_mcp_callable_but_main_is_mocked(monkeypatch):
    """The hidden command still runs; we mock the MCP main() so it returns."""
    import pantry_cooking_vibes.mcp_server.server as srv

    called: dict[str, bool] = {}

    def fake_main():
        called["yes"] = True

    monkeypatch.setattr(srv, "main", fake_main)
    res = runner.invoke(app, ["serve-mcp"])
    assert res.exit_code == 0
    assert called.get("yes") is True


@pytest.mark.parametrize("bad_arg", ["", "Bad", "1source", "has space"])
def test_ingest_source_validation_rejects_bad_names(db_path: Path, tmp_path: Path, bad_arg: str):
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text("", encoding="utf-8")
    res = runner.invoke(app, ["ingest", str(jsonl), "--source", bad_arg, "--db", str(db_path)])
    assert res.exit_code == 1
