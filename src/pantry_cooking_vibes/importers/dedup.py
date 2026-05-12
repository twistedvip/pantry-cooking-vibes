"""Recipe duplicate detection for the JSONL importer.

HungryRoot scrapes commonly produce multiple records for the same dish at
different serving sizes ("Chicken Fajitas, 2 servings" / "...4 servings").
The importer treats these as a single logical recipe and keeps only the
best variant.

Detection strategy (same source only — cross-source dedup has materially
different precision tradeoffs and is intentionally out of scope):

1. Normalize each candidate name: lowercase, fold accents, strip serving-
   size suffixes ("- 2 servings", "(serves 4)", "for 6"), collapse
   whitespace.
2. Hash the first ~500 chars of normalized instructions. If two records
   share both normalized name and instructions hash, they are duplicates.
3. Fall back to ``rapidfuzz.fuzz.WRatio``: name ≥ 92 AND instructions
   ≥ 85 → duplicate. Both signals required because "Chicken Fajitas" and
   "Chicken Fajita Bowls" score in the high 80s on name alone but are
   different dishes; instructions disambiguate.

Best-variant selection within a duplicate group:
  ``rating_count`` (popularity) > ``rating`` (quality) > instructions
  length (completeness) > original input order (stable tiebreaker).

Thresholds are deliberately conservative — false negatives leave a
duplicate in the DB (curator can delete via the web UI); false positives
silently drop a real recipe (much worse). 92 / 85 was chosen so adjacent
HR servings collide while genuinely different dishes don't.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from pantry_cooking_vibes.models import RecipeRecord

log = logging.getLogger(__name__)

NAME_FUZZY_THRESHOLD = 92.0
INSTRUCTIONS_FUZZY_THRESHOLD = 85.0
INSTRUCTIONS_HASH_PREFIX_CHARS = 500

# Editorial markers that follow the dish name on HungryRoot and similar
# sites. Run AFTER lowercasing.
#  * "chicken fajitas, 2 servings"      → "chicken fajitas"
#  * "chicken fajitas (serves 4)"       → "chicken fajitas"
#  * "chicken fajitas - for 6"          → "chicken fajitas"
#  * "chicken fajitas | family size"    → "chicken fajitas"
_SERVING_SUFFIX_RE = re.compile(
    r"\s*[-,|(\[]?\s*"
    r"(?:for\s+\d+|serves?\s+\d+|\d+\s*(?:-\s*\d+\s*)?servings?|"
    r"family\s+size|single\s+serve|\d+\s*portions?)"
    r"\s*[)\]]?\s*$",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")


def _fold(s: str) -> str:
    """NFKD-fold accents → ASCII (jalapeño → jalapeno)."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    """Lowercase, fold accents, strip serving-size suffix, collapse whitespace."""
    s = _fold(name).lower().strip()
    # Strip up to two trailing serving suffixes ("recipe, serves 4, for 6").
    for _ in range(2):
        new = _SERVING_SUFFIX_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _instructions_signature(instructions: str | None) -> str:
    """SHA1 of folded, lowercased, whitespace-collapsed instruction prefix.

    Numbers are kept (not stripped) — same dish at different servings
    typically has nearly-identical text but the *exact* prefix may differ
    by a digit. Hash equality is the strict path; fuzzy is the fallback.
    """
    if not instructions:
        return ""
    s = _fold(instructions).lower()
    s = _WS_RE.sub(" ", s).strip()
    s = s[:INSTRUCTIONS_HASH_PREFIX_CHARS]
    return hashlib.sha1(s.encode("utf-8"), usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class _Fingerprint:
    norm_name: str
    instructions_hash: str
    instructions_text: str  # first prefix, lowercased+folded, for fuzzy fallback


def _fingerprint(rec: RecipeRecord) -> _Fingerprint:
    instr = rec.instructions_md or ""
    folded_instr = _WS_RE.sub(" ", _fold(instr).lower()).strip()[
        :INSTRUCTIONS_HASH_PREFIX_CHARS
    ]
    return _Fingerprint(
        norm_name=normalize_name(rec.name),
        instructions_hash=_instructions_signature(instr),
        instructions_text=folded_instr,
    )


def _quality_key(rec: RecipeRecord) -> tuple[int, float, int]:
    """Sort key: higher rating_count, then higher rating, then longer instructions.

    All Nones treated as zero so they lose to any populated value.
    """
    return (
        rec.rating_count or 0,
        rec.rating or 0.0,
        len(rec.instructions_md or ""),
    )


def _is_duplicate(fp_a: _Fingerprint, fp_b: _Fingerprint) -> bool:
    """Two fingerprints describe the same logical recipe."""
    if not fp_a.norm_name or not fp_b.norm_name:
        return False
    if fp_a.norm_name == fp_b.norm_name and fp_a.instructions_hash == fp_b.instructions_hash:
        # Pure exact match on the cheap signals — almost certainly the same
        # dish (e.g. two HR variants where instructions differ only past
        # the prefix window).
        return True
    name_score = fuzz.WRatio(fp_a.norm_name, fp_b.norm_name)
    if name_score < NAME_FUZZY_THRESHOLD:
        return False
    if not fp_a.instructions_text or not fp_b.instructions_text:
        # Name fuzzy-matched but no instructions to disambiguate. Be
        # conservative — keep both. URL imports without instructions
        # would otherwise collapse on title alone.
        return False
    # token_sort_ratio is right for the instructions axis: HR serving
    # variants share ~all tokens (only quantities change) and score in
    # the high 90s, while genuinely different cooking methods that
    # happen to share a few words ("chicken", "garlic butter") drop
    # under 60. WRatio over-credited those shared keywords and produced
    # false positives at the original 85 cutoff.
    instr_score = fuzz.token_sort_ratio(fp_a.instructions_text, fp_b.instructions_text)
    return instr_score >= INSTRUCTIONS_FUZZY_THRESHOLD


@dataclass
class DedupDecision:
    """Per-cluster outcome for logging and stats reporting."""

    keeper: RecipeRecord
    losers: list[RecipeRecord]


def cluster_duplicates(records: list[RecipeRecord]) -> list[DedupDecision]:
    """Group near-duplicate records and elect a best variant per cluster.

    O(n²) on cluster size — fine for the scale this importer sees (a few
    thousand records per source per ingest). Stable: clusters preserve
    input order, and ties in ``_quality_key`` resolve to the first record
    encountered.
    """
    fps = [_fingerprint(r) for r in records]
    cluster_id: list[int] = [-1] * len(records)
    clusters: dict[int, list[int]] = {}
    next_id = 0
    for i, fp_i in enumerate(fps):
        for j in range(i):
            if cluster_id[j] == -1:
                continue
            if _is_duplicate(fp_i, fps[j]):
                cid = cluster_id[j]
                cluster_id[i] = cid
                clusters[cid].append(i)
                break
        if cluster_id[i] == -1:
            cluster_id[i] = next_id
            clusters[next_id] = [i]
            next_id += 1

    decisions: list[DedupDecision] = []
    for indices in clusters.values():
        # Keep highest-quality; stable on ties via min index.
        indices_sorted = sorted(
            indices,
            key=lambda idx: (_quality_key(records[idx]), -idx),
            reverse=True,
        )
        keeper_idx = indices_sorted[0]
        loser_idxs = indices_sorted[1:]
        decisions.append(
            DedupDecision(
                keeper=records[keeper_idx],
                losers=[records[k] for k in loser_idxs],
            )
        )
    return decisions
