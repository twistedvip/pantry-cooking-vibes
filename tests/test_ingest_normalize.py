"""Auto-normalization on `meal-cli ingest`.

The ingest command runs `backfill_recipe_canonicals` after a successful
write unless `--no-normalize` is passed. Normalization is skipped on
`--dry-run` and when no recipes were written.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pantry_cooking_vibes.cli import app as cli_app


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _recipe(source_id: str, name: str) -> dict:
    return {
        "source_id": source_id,
        "name": name,
        "instructions_md": "Mix and serve.",
        "image_url": "https://cdn.example.com/soup.jpg",
        "ingredients": [
            {"original_text": "1 onion, sliced"},
            {"original_text": "2 cloves garlic"},
        ],
    }


def test_ingest_normalizes_by_default(tmp_path, db_path):
    jsonl = tmp_path / "recipes.jsonl"
    _write_jsonl(jsonl, [_recipe("r1", "Onion Soup")])

    result = CliRunner().invoke(
        cli_app, ["ingest", str(jsonl), "--source", "manual", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    assert "ingest done" in result.output
    assert "normalize done" in result.output


def test_ingest_no_normalize_flag_skips(tmp_path, db_path):
    jsonl = tmp_path / "recipes.jsonl"
    _write_jsonl(jsonl, [_recipe("r1", "Onion Soup")])

    result = CliRunner().invoke(
        cli_app,
        ["ingest", str(jsonl), "--source", "manual", "--no-normalize", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "ingest done" in result.output
    assert "normalize done" not in result.output


def test_ingest_dry_run_skips_normalize(tmp_path, db_path):
    jsonl = tmp_path / "recipes.jsonl"
    _write_jsonl(jsonl, [_recipe("r1", "Onion Soup")])

    result = CliRunner().invoke(
        cli_app,
        ["ingest", str(jsonl), "--source", "manual", "--dry-run", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "normalize: skipped (dry-run)" in result.output
    assert "normalize done" not in result.output


def test_ingest_skips_normalize_when_no_recipes_written(tmp_path, db_path):
    # Every line invalid (missing required `name`) -> stats['recipes'] == 0.
    jsonl = tmp_path / "recipes.jsonl"
    _write_jsonl(jsonl, [{"source_id": "bad1", "ingredients": []}])

    result = CliRunner().invoke(
        cli_app, ["ingest", str(jsonl), "--source", "manual", "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    assert "normalize: skipped (no recipes written)" in result.output
    assert "normalize done" not in result.output
