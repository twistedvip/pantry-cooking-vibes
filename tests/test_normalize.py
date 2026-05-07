"""Tests for the ingredient normalization layer."""

from __future__ import annotations

from pantry_cooking_vibes.db import connect
from pantry_cooking_vibes.importers.normalize import (
    AUTO_APPROVE_THRESHOLD,
    PROPOSE_THRESHOLD,
    _build_choice_map,
    _clean,
    _load_index,
    approve_mapping,
    match_one,
    normalize_text,
    reject_mapping,
    review_pending,
    run_normalization,
    upsert_mapping,
)

# ---------- pure helpers ----------


def test_clean_strips_brand_and_packaging_words():
    assert _clean("Organic Broccoli Florets, 1 bag") == "broccoli , 1"
    assert _clean("Boneless Skinless Chicken Breast") == "chicken breast"


def test_clean_handles_empty_and_whitespace():
    assert _clean("") == ""
    assert _clean("   ") == ""


def test_clean_folds_unicode_accents():
    """NFKD + combining-mark strip so accented chars match ASCII canonicals."""
    assert _clean("jalapeño") == "jalapeno"
    assert _clean("crème brûlée") == "creme brulee"
    assert _clean("Café") == "cafe"


def test_match_one_finds_canonical_for_accented_input(db_path):
    """The seeded canonical 'jalapeno' (no accent) should match input 'jalapeño'."""
    with connect(db_path) as conn:
        index = _load_index(conn)
    choices, choice_to_id = _build_choice_map(index)
    canonical_id, score = match_one("jalapeño", choices, choice_to_id)
    assert canonical_id is not None
    # Lookup the canonical name for clarity in failure messages
    with connect(db_path) as conn:
        name = conn.execute(
            "SELECT name FROM canonical_ingredients WHERE id = ?", (canonical_id,)
        ).fetchone()["name"]
    assert name == "jalapeno"
    assert score >= AUTO_APPROVE_THRESHOLD  # exact accent-folded match → very high


def test_thresholds_make_sense():
    assert AUTO_APPROVE_THRESHOLD > PROPOSE_THRESHOLD
    assert PROPOSE_THRESHOLD >= 70.0  # noise floor for WRatio review queues


# ---------- index + choice map ----------


def test_load_index_orders_by_id(db_path):
    with connect(db_path) as conn:
        index = _load_index(conn)
    ids = [e["id"] for e in index]
    assert ids == sorted(ids)


def test_build_choice_map_first_write_wins_and_warns(caplog):
    """Two canonicals sharing an alias: lower id keeps it, collision is logged."""
    index = [
        {"id": 1, "name": "white rice", "aliases": ["rice"], "choices": ["white rice", "rice"]},
        {"id": 2, "name": "brown rice", "aliases": ["rice"], "choices": ["brown rice", "rice"]},
    ]
    import logging

    with caplog.at_level(logging.WARNING, logger="pantry_cooking_vibes.importers.normalize"):
        choices, choice_to_id = _build_choice_map(index)
    assert choice_to_id["rice"] == 1  # lower id wins
    assert "collision" in caplog.text.lower()
    assert choices.count("rice") == 1  # not duplicated


# ---------- matching ----------


def test_match_one_returns_none_for_empty():
    assert match_one("", ["broccoli"], {"broccoli": 1}) == (None, 0.0)


def test_normalize_text_status_buckets(db_path):
    """exact match -> approved, partial -> proposed or no_match by threshold."""
    with connect(db_path) as conn:
        index = _load_index(conn)
        choices, choice_to_id = _build_choice_map(index)

    # exact-ish match should auto-approve
    r = normalize_text("broccoli", choices, choice_to_id)
    assert r.status == "approved"
    assert r.confidence >= 0.90
    assert r.proposed_canonical_id is not None

    # gibberish should not match
    r = normalize_text("zzzzzzzzz qwerty", choices, choice_to_id)
    assert r.status == "no_match"
    assert r.proposed_canonical_id is None
    assert r.confidence == 0.0


# ---------- DB upsert + queue ----------


def test_upsert_mapping_inserts_then_no_op_without_overwrite(db_path):
    with connect(db_path) as conn:
        index = _load_index(conn)
        choices, choice_to_id = _build_choice_map(index)
        r = normalize_text("broccoli", choices, choice_to_id)
        r.source_key = "broccoli-test-1"
        upsert_mapping(conn, "test-source", r)
        # second call should not duplicate
        upsert_mapping(conn, "test-source", r)

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM ingredient_mapping_queue WHERE source_key='broccoli-test-1'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "approved"


def test_upsert_mapping_no_match_status_coerced_to_proposed(db_path):
    """Schema CHECK only allows proposed/approved/rejected. no_match -> proposed."""
    with connect(db_path) as conn:
        index = _load_index(conn)
        choices, choice_to_id = _build_choice_map(index)
        r = normalize_text("zzzzzzzzzz", choices, choice_to_id)
        assert r.status == "no_match"
        r.source_key = "garbage-1"
        upsert_mapping(conn, "test-source", r)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM ingredient_mapping_queue WHERE source_key='garbage-1'"
        ).fetchone()
    assert row["status"] == "proposed"


def test_upsert_mapping_overwrite_updates_existing(db_path):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO ingredient_mapping_queue (source, source_key, original_text, "
            "proposed_canonical_id, confidence, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("test-source", "key-x", "original", None, 0.0, "proposed"),
        )

    with connect(db_path) as conn:
        index = _load_index(conn)
        choices, choice_to_id = _build_choice_map(index)
        r = normalize_text("broccoli", choices, choice_to_id)
        r.source_key = "key-x"
        upsert_mapping(conn, "test-source", r, overwrite=True)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, proposed_canonical_id, confidence "
            "FROM ingredient_mapping_queue WHERE source_key='key-x'"
        ).fetchone()
    assert row["status"] == "approved"
    assert row["proposed_canonical_id"] is not None
    assert row["confidence"] >= 0.90


# ---------- run_normalization batch ----------


def test_run_normalization_buckets_results(db_path):
    items = [
        {"source_key": "broccoli-x", "original_text": "Broccoli Florets"},
        {"source_key": "garbage-x", "original_text": "zzzzzzzzz qwerty"},
    ]
    stats = run_normalization("test-source", items, db_path=db_path, quiet=True)
    assert stats["total"] == 2
    assert stats["approved"] + stats["proposed"] + stats["no_match"] == 2


# ---------- review/approve/reject workflow ----------


def test_review_approve_reject_round_trip(db_path):
    items = [
        {"source_key": "broccoli-rt", "original_text": "broccoli"},
        {"source_key": "garbage-rt", "original_text": "zzzzzzzzz"},
    ]
    run_normalization("test-source", items, db_path=db_path, quiet=True)

    pending = review_pending(db_path=db_path)
    # the no-match row appears in pending (status='proposed' due to schema coercion)
    targets = {p["source_key"]: p for p in pending}
    assert "garbage-rt" in targets

    queue_id = targets["garbage-rt"]["id"]
    reject_mapping(queue_id, db_path=db_path)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM ingredient_mapping_queue WHERE id=?", (queue_id,)
        ).fetchone()
    assert row["status"] == "rejected"

    # Approve a canonical for it
    with connect(db_path) as conn:
        canonical_id = conn.execute(
            "SELECT id FROM canonical_ingredients ORDER BY id LIMIT 1"
        ).fetchone()[0]
    approve_mapping(queue_id, canonical_id, db_path=db_path)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, proposed_canonical_id FROM ingredient_mapping_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    assert row["status"] == "approved"
    assert row["proposed_canonical_id"] == canonical_id


# ---------- backfill_recipe_canonicals ----------


def _add_recipe_with_ingredient(conn, name: str, ingredient_text: str) -> int:
    rid = conn.execute(
        "INSERT INTO recipes (source, source_id, name) VALUES ('test-source', ?, ?) RETURNING id",
        (f"hf://{name}", name),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO recipe_ingredients (recipe_id, original_text) VALUES (?, ?)",
        (rid, ingredient_text),
    )
    return rid


def test_backfill_recipe_canonicals_writes_high_score_match(db_path):
    from pantry_cooking_vibes.importers.normalize import backfill_recipe_canonicals

    with connect(db_path) as conn:
        guac_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name='guacamole'"
        ).fetchone()[0]
        _add_recipe_with_ingredient(conn, "Burrito Bowl", "Fresh Guacamole 4-Pack (Calavo)")
        _add_recipe_with_ingredient(conn, "Tacos", "Fresh Guacamole 4-Pack (Calavo)")

    stats = backfill_recipe_canonicals(db_path=db_path, quiet=True)
    assert stats["distinct_texts"] == 1
    assert stats["approved"] == 1
    assert stats["rows_updated"] == 2

    with connect(db_path) as conn:
        rows = conn.execute("SELECT canonical_id FROM recipe_ingredients ORDER BY id").fetchall()
    assert all(r["canonical_id"] == guac_id for r in rows)


def test_backfill_recipe_canonicals_skips_already_mapped_rows(db_path):
    from pantry_cooking_vibes.importers.normalize import backfill_recipe_canonicals

    with connect(db_path) as conn:
        guac_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name='guacamole'"
        ).fetchone()[0]
        bogus_id = conn.execute(
            "INSERT INTO canonical_ingredients (name, category) "
            "VALUES ('not-real-test-canonical', 'misc') RETURNING id"
        ).fetchone()[0]
        rid = conn.execute(
            "INSERT INTO recipes (source, source_id, name) "
            "VALUES ('test-source', 'hf://x', 'X') RETURNING id"
        ).fetchone()[0]
        # One row pre-mapped to a wrong canonical, one row unmapped.
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, canonical_id, original_text) "
            "VALUES (?, ?, ?)",
            (rid, bogus_id, "Fresh Guacamole 4-Pack (Calavo)"),
        )
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, original_text) VALUES (?, ?)",
            (rid, "Fresh Guacamole 4-Pack (Calavo)"),
        )

    backfill_recipe_canonicals(db_path=db_path, quiet=True)

    with connect(db_path) as conn:
        rows = [
            r["canonical_id"]
            for r in conn.execute("SELECT canonical_id FROM recipe_ingredients ORDER BY id")
        ]
    assert rows == [bogus_id, guac_id]


def test_backfill_recipe_canonicals_skips_low_confidence(db_path):
    from pantry_cooking_vibes.importers.normalize import backfill_recipe_canonicals

    with connect(db_path) as conn:
        _add_recipe_with_ingredient(conn, "Mystery", "completely unrelated foodstuff xyz")

    stats = backfill_recipe_canonicals(db_path=db_path, quiet=True)
    assert stats["approved"] == 0
    assert stats["rows_updated"] == 0
    with connect(db_path) as conn:
        cid = conn.execute("SELECT canonical_id FROM recipe_ingredients").fetchone()[0]
    assert cid is None


def test_backfill_queues_proposed_and_no_match_for_review(db_path):
    """Borderline + no-match texts must land in the review queue under
    source 'recipe_ingredient_text' so review-mappings shows them."""
    from pantry_cooking_vibes.importers.normalize import (
        RECIPE_TEXT_SOURCE,
        backfill_recipe_canonicals,
    )

    with connect(db_path) as conn:
        # Pick a real seeded canonical so the no-match path is unambiguous.
        _add_recipe_with_ingredient(conn, "Mystery", "completely unrelated foodstuff xyz")

    backfill_recipe_canonicals(db_path=db_path, quiet=True)

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source_key, status FROM ingredient_mapping_queue WHERE source = ?",
            (RECIPE_TEXT_SOURCE,),
        ).fetchall()
    keys = {r["source_key"]: r["status"] for r in rows}
    assert "completely unrelated foodstuff xyz" in keys
    # 'no_match' is coerced to 'proposed' by upsert_mapping for queue display.
    assert keys["completely unrelated foodstuff xyz"] == "proposed"


def test_apply_text_mappings_writes_canonical_after_approval(db_path):
    from pantry_cooking_vibes.importers.normalize import (
        RECIPE_TEXT_SOURCE,
        apply_text_mappings,
        approve_mapping,
        backfill_recipe_canonicals,
    )

    with connect(db_path) as conn:
        # Use a canonical and a text whose initial match is below auto-approve.
        rid = conn.execute(
            "INSERT INTO recipes (source, source_id, name) "
            "VALUES ('test-source', 'hf://x', 'X') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, original_text) VALUES (?, ?)",
            (rid, "weird mystery thing"),
        )
        target_id = conn.execute(
            "SELECT id FROM canonical_ingredients WHERE name='guacamole'"
        ).fetchone()[0]

    backfill_recipe_canonicals(db_path=db_path, quiet=True)

    with connect(db_path) as conn:
        queue_id = conn.execute(
            "SELECT id FROM ingredient_mapping_queue "
            "WHERE source = ? AND source_key = 'weird mystery thing'",
            (RECIPE_TEXT_SOURCE,),
        ).fetchone()[0]

    approve_mapping(queue_id, target_id, db_path=db_path)
    stats = apply_text_mappings(db_path=db_path, quiet=True)
    assert stats["approved_queue_rows"] == 1
    assert stats["rows_updated"] == 1

    with connect(db_path) as conn:
        cid = conn.execute(
            "SELECT canonical_id FROM recipe_ingredients WHERE recipe_id = ?", (rid,)
        ).fetchone()[0]
    assert cid == target_id
