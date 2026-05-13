"""Dedup pass for the JSONL importer.

These tests exercise the real ingest pipeline (no monkey-patching of the
dedup logic). The fixtures emulate the actual shape HungryRoot scrapes
produce: same dish at multiple serving sizes, different ratings, same
instructions modulo ingredient quantities.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.importers.dedup import (
    NAME_FUZZY_THRESHOLD,
    cluster_duplicates,
    normalize_name,
)
from pantry_cooking_vibes.importers.jsonl_ingest import ingest_jsonl
from pantry_cooking_vibes.models import RecipeRecord

# ---------- helpers ----------


def _hr_variant(
    *,
    source_id: str,
    name: str,
    servings: int,
    rating: float | None = 4.5,
    rating_count: int | None = 100,
    instructions: str | None = None,
    image_url: str = "https://cdn.hungryroot.com/images/fajitas.jpg",
) -> dict:
    """Realistic HungryRoot-style record. Instructions vary only in
    ingredient quantities — exactly the pattern that makes serving-size
    variants near-duplicates rather than exact-duplicates."""
    if instructions is None:
        # qty doubles with servings — same prose, different numbers,
        # which is what real HR data looks like.
        chicken_lb = servings * 0.25
        instructions = (
            "Heat 1 tablespoon oil in a skillet. "
            f"Add {chicken_lb:g} pounds chicken cut into strips and cook 6 minutes. "
            "Add 1 sliced bell pepper and 1 sliced onion; sauté 4 minutes. "
            "Stir in fajita seasoning, then serve in warmed tortillas with lime."
        )
    return {
        "source_id": source_id,
        "name": name,
        "servings": servings,
        "cooking_time_min": 25,
        "instructions_md": instructions,
        "image_url": image_url,
        "rating": rating,
        "rating_count": rating_count,
        "ingredients": [
            {"original_text": f"{servings * 0.25:g} lb chicken breast"},
            {"original_text": "1 bell pepper, sliced"},
            {"original_text": "1 onion, sliced"},
            {"original_text": "fajita seasoning"},
            {"original_text": f"{servings} tortillas"},
        ],
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ---------- normalize_name ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Chicken Fajitas, 4 servings", "chicken fajitas"),
        ("Chicken Fajitas, 2 servings", "chicken fajitas"),
        ("Chicken Fajitas (serves 6)", "chicken fajitas"),
        ("Chicken Fajitas - for 4", "chicken fajitas"),
        ("Chicken Fajitas | Family Size", "chicken fajitas"),
        ("Jalapeño Chicken Fajitas", "jalapeno chicken fajitas"),
        ("  Chicken   Fajitas  ", "chicken fajitas"),
        # Real-world: "10 servings" two-digit case
        ("Beef Stew, 10 servings", "beef stew"),
        # Mid-string serving counts must NOT be stripped.
        ("Chicken with 2 sauces", "chicken with 2 sauces"),
    ],
)
def test_normalize_name_strips_serving_suffixes(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


# ---------- cluster_duplicates (pure function) ----------


def _rec(**kw) -> RecipeRecord:
    """Pydantic-validated RecipeRecord with sensible defaults for unit tests."""
    base = {
        "source_id": kw.pop("source_id", "x"),
        "name": kw.pop("name", "X"),
        "ingredients": kw.pop("ingredients", []),
    }
    base.update(kw)
    return RecipeRecord.model_validate(base)


def test_cluster_groups_serving_size_variants() -> None:
    """The HR pattern: same name modulo ", N servings"; identical instructions."""
    records = [
        _rec(
            source_id="hr-fajitas-2",
            name="Chicken Fajitas, 2 servings",
            instructions_md=(
                "Heat oil in a skillet. Add chicken and cook 6 minutes. "
                "Add bell pepper and onion. Stir in seasoning, serve."
            ),
            rating=4.6,
            rating_count=42,
        ),
        _rec(
            source_id="hr-fajitas-4",
            name="Chicken Fajitas, 4 servings",
            instructions_md=(
                "Heat oil in a skillet. Add chicken and cook 6 minutes. "
                "Add bell pepper and onion. Stir in seasoning, serve."
            ),
            rating=4.7,
            rating_count=300,  # most popular -> winner
        ),
        _rec(
            source_id="hr-fajitas-6",
            name="Chicken Fajitas (serves 6)",
            instructions_md=(
                "Heat oil in a skillet. Add chicken and cook 6 minutes. "
                "Add bell pepper and onion. Stir in seasoning, serve."
            ),
            rating=4.7,
            rating_count=120,
        ),
    ]
    decisions = cluster_duplicates(records)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.keeper.source_id == "hr-fajitas-4"
    assert {loser.source_id for loser in d.losers} == {"hr-fajitas-2", "hr-fajitas-6"}


def test_cluster_keeps_genuinely_different_dishes() -> None:
    """High-overlap names but different dishes must NOT collapse.

    'Chicken Fajitas' vs 'Chicken Fajita Bowls' — fuzzy name score is in
    the high 80s, intentionally below the 92 cutoff. Same protein, same
    aromatics, but the bowl version has rice in the instructions.
    """
    records = [
        _rec(
            source_id="hr-fajitas",
            name="Chicken Fajitas",
            instructions_md=("Heat oil. Cook chicken with onion and pepper. Serve in tortillas."),
        ),
        _rec(
            source_id="hr-fajita-bowls",
            name="Chicken Fajita Bowls",
            instructions_md=("Cook rice. Sauté chicken with peppers. Layer over rice with salsa."),
        ),
    ]
    decisions = cluster_duplicates(records)
    kept = {d.keeper.source_id for d in decisions}
    assert kept == {"hr-fajitas", "hr-fajita-bowls"}, f"expected both to survive; got {decisions}"


def test_cluster_requires_instructions_overlap_for_fuzzy_match() -> None:
    """Names match >= threshold but instructions diverge -> NOT a duplicate.

    Defends against false positives when two scrapes happen to share a
    title pattern but describe genuinely different prep.
    """
    name = "Garlic Butter Chicken"
    assert NAME_FUZZY_THRESHOLD <= 100  # sanity
    records = [
        _rec(
            source_id="a",
            name=name,
            instructions_md="Pan-fry chicken in garlic butter for 8 minutes per side.",
        ),
        _rec(
            source_id="b",
            name=name,
            instructions_md=(
                "Slow-cook chicken in a Dutch oven with stock and herbs for 3 hours, "
                "then shred and toss with garlic butter sauce."
            ),
        ),
    ]
    decisions = cluster_duplicates(records)
    assert len(decisions) == 2, "different cooking methods should not be deduped on name alone"


def test_cluster_best_variant_tiebreaks_on_quality_then_input_order() -> None:
    """rating_count first, then rating, then instructions length, then input order."""
    same_instructions = "Whisk eggs, cook in a buttered skillet over low heat, stirring."
    records = [
        _rec(
            source_id="first",
            name="Soft Scramble, 1 serving",
            instructions_md=same_instructions,
            rating=4.0,
            rating_count=50,
        ),
        _rec(
            source_id="best",
            name="Soft Scramble, 2 servings",
            instructions_md=same_instructions,
            rating=4.9,
            rating_count=900,  # winner
        ),
        _rec(
            source_id="rated-but-unpopular",
            name="Soft Scramble (serves 4)",
            instructions_md=same_instructions,
            rating=5.0,
            rating_count=2,
        ),
    ]
    decisions = cluster_duplicates(records)
    assert len(decisions) == 1
    assert decisions[0].keeper.source_id == "best"


# ---------- end-to-end ingest ----------


def test_ingest_dedup_default_picks_best_hungryroot_variant(db_path: Path, tmp_path: Path) -> None:
    """Realistic HungryRoot scrape: 3 serving-size variants of the same
    dish. Default ingest must keep only the highest rating_count one."""
    jsonl = tmp_path / "hr.jsonl"
    _write_jsonl(
        jsonl,
        [
            _hr_variant(
                source_id="hr-fajitas-2",
                name="Chicken Fajitas, 2 servings",
                servings=2,
                rating=4.6,
                rating_count=42,
            ),
            _hr_variant(
                source_id="hr-fajitas-4",
                name="Chicken Fajitas, 4 servings",
                servings=4,
                rating=4.7,
                rating_count=300,
            ),
            _hr_variant(
                source_id="hr-fajitas-6",
                name="Chicken Fajitas (serves 6)",
                servings=6,
                rating=4.7,
                rating_count=120,
            ),
        ],
    )
    stats = ingest_jsonl(jsonl, "hungryroot", db_path=db_path, quiet=True)
    assert stats["processed"] == 3
    assert stats["recipes"] == 1
    assert stats["duplicates_skipped"] == 2

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source_id, servings FROM recipes WHERE source = 'hungryroot'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source_id"] == "hr-fajitas-4"
    assert rows[0]["servings"] == 4


def test_ingest_no_dedup_imports_every_variant(db_path: Path, tmp_path: Path) -> None:
    """--no-dedup escape hatch: same JSONL, every record kept."""
    jsonl = tmp_path / "hr.jsonl"
    _write_jsonl(
        jsonl,
        [
            _hr_variant(source_id="hr-fajitas-2", name="Chicken Fajitas, 2 servings", servings=2),
            _hr_variant(source_id="hr-fajitas-4", name="Chicken Fajitas, 4 servings", servings=4),
            _hr_variant(source_id="hr-fajitas-6", name="Chicken Fajitas (serves 6)", servings=6),
        ],
    )
    stats = ingest_jsonl(jsonl, "hungryroot", db_path=db_path, quiet=True, dedup=False)
    assert stats["processed"] == 3
    assert stats["recipes"] == 3
    assert stats["duplicates_skipped"] == 0

    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM recipes WHERE source = 'hungryroot'").fetchone()[
            0
        ]
    assert count == 3


def test_ingest_dedup_against_existing_db_rows(db_path: Path, tmp_path: Path) -> None:
    """Re-running ingest must not import a new variant when an existing
    DB row is already a near-duplicate. Otherwise dedup would be a one-
    shot pass and re-scrapes would recreate duplicates."""
    # Seed: a popular fajitas variant already in the DB.
    seed = tmp_path / "seed.jsonl"
    _write_jsonl(
        seed,
        [
            _hr_variant(
                source_id="hr-fajitas-4",
                name="Chicken Fajitas, 4 servings",
                servings=4,
                rating=4.9,
                rating_count=1000,
            ),
        ],
    )
    ingest_jsonl(seed, "hungryroot", db_path=db_path, quiet=True)

    # Second scrape pass: the 2-serving variant arrives.
    second = tmp_path / "second.jsonl"
    _write_jsonl(
        second,
        [
            _hr_variant(
                source_id="hr-fajitas-2",
                name="Chicken Fajitas, 2 servings",
                servings=2,
                rating=4.7,
                rating_count=50,
            ),
        ],
    )
    stats = ingest_jsonl(second, "hungryroot", db_path=db_path, quiet=True)
    assert stats["processed"] == 1
    assert stats["recipes"] == 0
    assert stats["duplicates_skipped"] == 1

    with connect(db_path) as conn:
        rows = conn.execute("SELECT source_id FROM recipes WHERE source = 'hungryroot'").fetchall()
    assert {r["source_id"] for r in rows} == {"hr-fajitas-4"}


def test_ingest_dedup_does_not_collapse_distinct_dishes(db_path: Path, tmp_path: Path) -> None:
    """Two different fajita-family dishes must both import — the
    threshold is calibrated against this kind of pair."""
    jsonl = tmp_path / "hr.jsonl"
    _write_jsonl(
        jsonl,
        [
            _hr_variant(
                source_id="hr-fajitas",
                name="Chicken Fajitas",
                servings=4,
                instructions=(
                    "Heat oil in a skillet. Add chicken and cook 6 minutes. "
                    "Add bell pepper and onion. Stir in seasoning, serve in tortillas."
                ),
            ),
            _hr_variant(
                source_id="hr-fajita-bowls",
                name="Chicken Fajita Bowls",
                servings=4,
                instructions=(
                    "Cook rice and set aside. Sauté chicken with peppers and onion. "
                    "Layer over rice with salsa, sour cream, and cilantro."
                ),
            ),
        ],
    )
    stats = ingest_jsonl(jsonl, "hungryroot", db_path=db_path, quiet=True)
    assert stats["recipes"] == 2
    assert stats["duplicates_skipped"] == 0


def test_ingest_dry_run_writes_nothing(db_path: Path, tmp_path: Path) -> None:
    """Dry-run mode must compute stats (so a curator can see what would
    happen) but leave the DB untouched."""
    jsonl = tmp_path / "hr.jsonl"
    _write_jsonl(
        jsonl,
        [
            _hr_variant(source_id="hr-fajitas-2", name="Chicken Fajitas, 2 servings", servings=2),
            _hr_variant(source_id="hr-fajitas-4", name="Chicken Fajitas, 4 servings", servings=4),
        ],
    )
    stats = ingest_jsonl(jsonl, "hungryroot", db_path=db_path, quiet=True, dry_run=True)
    assert stats["processed"] == 2
    assert stats["recipes"] == 1  # what *would* have been imported
    assert stats["duplicates_skipped"] == 1

    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    assert count == 0


def test_ingest_dedup_skips_records_without_image(db_path: Path, tmp_path: Path) -> None:
    """Existing image-URL gate still wins over dedup — a duplicate with
    no image is dropped via the invalid-skipped path, not duplicates."""
    jsonl = tmp_path / "hr.jsonl"
    rec_no_image = _hr_variant(
        source_id="hr-fajitas-no-img",
        name="Chicken Fajitas, 4 servings",
        servings=4,
        image_url="",
    )
    _write_jsonl(
        jsonl,
        [
            rec_no_image,
            _hr_variant(
                source_id="hr-fajitas-good", name="Chicken Fajitas, 4 servings", servings=4
            ),
        ],
    )
    stats = ingest_jsonl(jsonl, "hungryroot", db_path=db_path, quiet=True)
    assert stats["skipped"] == 1
    assert stats["recipes"] == 1
    assert stats["duplicates_skipped"] == 0


def test_cluster_blocks_on_norm_name_even_with_identical_instructions() -> None:
    """Different normalized names must not cluster even if instructions match.

    Locks the exact-name blocking optimization: cross-bucket fuzzy-name
    matches are out of scope, so records that only collide on instructions
    stay in separate clusters.
    """
    shared_instructions = "Heat oil; cook chicken; serve."
    rec_a = RecipeRecord.model_validate(
        _hr_variant(
            source_id="a",
            name="Chicken Fajitas",
            servings=2,
            instructions=shared_instructions,
        )
    )
    rec_b = RecipeRecord.model_validate(
        _hr_variant(
            source_id="b",
            name="Beef Stew",
            servings=2,
            instructions=shared_instructions,
        )
    )
    decisions = cluster_duplicates([rec_a, rec_b])
    assert len(decisions) == 2
    assert {d.keeper.source_id for d in decisions} == {"a", "b"}
    assert all(d.losers == [] for d in decisions)


def test_cluster_progress_callback_fires() -> None:
    """progress callback is invoked at progress_every and once at completion."""
    recs = [
        RecipeRecord.model_validate(_hr_variant(source_id=f"r{i}", name=f"Recipe {i}", servings=2))
        for i in range(5)
    ]
    calls: list[tuple[int, int]] = []
    cluster_duplicates(
        recs,
        progress=lambda done, total: calls.append((done, total)),
        progress_every=2,
    )
    # Ticks at i=2, 4 (every 2 iterations) plus a final (5, 5).
    assert (5, 5) in calls
    assert (2, 5) in calls
    assert (4, 5) in calls
