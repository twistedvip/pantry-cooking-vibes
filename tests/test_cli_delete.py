"""Tests for `meal-cli delete`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pantry_cooking_vibes.cli import app
from pantry_cooking_vibes.db import connect

runner = CliRunner()


def _seed_plan_and_pantry(db: Path) -> tuple[int, int, int]:
    """Return (recipe_id, plan_id, pantry_id) from the seeded fixture extras."""
    with connect(db) as conn:
        recipe_id = conn.execute(
            "SELECT id FROM recipes WHERE name = 'Broccoli Stir Fry'"
        ).fetchone()["id"]
        plan_id = conn.execute(
            "INSERT INTO meal_plans (week_of) VALUES ('2026-03-01') RETURNING id"
        ).fetchone()["id"]
        pantry_id = conn.execute("SELECT id FROM pantry LIMIT 1").fetchone()["id"]
    return recipe_id, plan_id, pantry_id


# ---------- argument validation ----------


def test_delete_rejects_unknown_type(db_path):
    res = runner.invoke(app, ["delete", "bogus", "--db", str(db_path), "--yes"])
    assert res.exit_code == 1
    assert "invalid type" in res.output


def test_delete_requires_type_argument(db_path):
    res = runner.invoke(app, ["delete", "--db", str(db_path)])
    assert res.exit_code != 0


# ---------- single-item delete with --id ----------


def test_delete_recipe_by_id_with_yes(seeded_db_path):
    recipe_id, _, _ = _seed_plan_and_pantry(seeded_db_path)
    res = runner.invoke(
        app,
        ["delete", "recipe", "--id", str(recipe_id), "--yes", "--db", str(seeded_db_path)],
    )
    assert res.exit_code == 0, res.output
    assert f"deleted recipe {recipe_id}" in res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is None


def test_delete_plan_by_id_with_yes(seeded_db_path):
    _, plan_id, _ = _seed_plan_and_pantry(seeded_db_path)
    res = runner.invoke(
        app,
        ["delete", "plan", "--id", str(plan_id), "--yes", "--db", str(seeded_db_path)],
    )
    assert res.exit_code == 0, res.output
    assert f"deleted meal plan {plan_id}" in res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT 1 FROM meal_plans WHERE id = ?", (plan_id,)).fetchone() is None


def test_delete_pantry_by_id_with_yes(seeded_db_path):
    _, _, pantry_id = _seed_plan_and_pantry(seeded_db_path)
    res = runner.invoke(
        app,
        ["delete", "pantry", "--id", str(pantry_id), "--yes", "--db", str(seeded_db_path)],
    )
    assert res.exit_code == 0, res.output
    assert f"deleted pantry item {pantry_id}" in res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT 1 FROM pantry WHERE id = ?", (pantry_id,)).fetchone() is None


def test_delete_recipe_missing_id_exits_1(seeded_db_path):
    res = runner.invoke(
        app,
        ["delete", "recipe", "--id", "99999", "--yes", "--db", str(seeded_db_path)],
    )
    assert res.exit_code == 1
    assert "recipe 99999 not found" in res.output


# ---------- confirmation prompt ----------


def test_delete_recipe_prompts_and_aborts_on_no(seeded_db_path):
    recipe_id, _, _ = _seed_plan_and_pantry(seeded_db_path)
    res = runner.invoke(
        app,
        ["delete", "recipe", "--id", str(recipe_id), "--db", str(seeded_db_path)],
        input="n\n",
    )
    assert res.exit_code == 0
    assert "aborted" in res.output
    with connect(seeded_db_path) as conn:
        assert (
            conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is not None
        )


def test_delete_recipe_prompts_and_deletes_on_yes(seeded_db_path):
    recipe_id, _, _ = _seed_plan_and_pantry(seeded_db_path)
    res = runner.invoke(
        app,
        ["delete", "recipe", "--id", str(recipe_id), "--db", str(seeded_db_path)],
        input="y\n",
    )
    assert res.exit_code == 0, res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is None


# ---------- bulk delete (no --id) ----------


def test_delete_all_recipes_with_yes(seeded_db_path):
    res = runner.invoke(
        app,
        ["delete", "recipe", "--yes", "--db", str(seeded_db_path)],
    )
    assert res.exit_code == 0, res.output
    assert "deleted 2 recipe row(s)" in res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0
        # FTS stays in sync via row-level deletes.
        assert conn.execute("SELECT COUNT(*) FROM recipes_fts").fetchone()[0] == 0


def test_delete_all_plans_with_yes(seeded_db_path):
    _seed_plan_and_pantry(seeded_db_path)
    res = runner.invoke(
        app,
        ["delete", "plan", "--yes", "--db", str(seeded_db_path)],
    )
    assert res.exit_code == 0, res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meal_plans").fetchone()[0] == 0


def test_delete_all_pantry_with_yes(seeded_db_path):
    res = runner.invoke(
        app,
        ["delete", "pantry", "--yes", "--db", str(seeded_db_path)],
    )
    assert res.exit_code == 0, res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pantry").fetchone()[0] == 0


def test_delete_all_empty_table_short_circuits(db_path):
    res = runner.invoke(
        app,
        ["delete", "recipe", "--yes", "--db", str(db_path)],
    )
    assert res.exit_code == 0
    assert "no recipe rows to delete" in res.output


def test_delete_all_aborts_on_no(seeded_db_path):
    res = runner.invoke(
        app,
        ["delete", "recipe", "--db", str(seeded_db_path)],
        input="n\n",
    )
    assert res.exit_code == 0
    assert "aborted" in res.output
    with connect(seeded_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 2
