"""JSONL ingest contract models.

The application stores data in SQLite and reads/writes through ``db.connect()``
returning ``sqlite3.Row``. These pydantic models exist only to validate the
JSONL ingest wire format produced by external scrapers (see
``docs/jsonl_contract.md``). Storage-side rows are passed around as plain
dicts via ``mcp_server.tools._row_to_dict``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

# Free-form source names registered by importers (core or plugin).
# Lowercase ASCII letter to start, then lowercase letters / digits / hyphens.
SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class RecipeIngredientRecord(BaseModel):
    """JSONL ingest line shape for a single ingredient."""

    original_text: str | None = None
    quantity: float | None = None
    unit: str | None = None
    notes: str | None = None
    canonical_hint: str | None = None


class RecipeRecord(BaseModel):
    """JSONL ingest line shape — the contract scrapers must produce.

    See docs/jsonl_contract.md for the full spec. Required fields:
    ``source_id``, ``name``, ``ingredients`` (may be ``[]``). All other
    fields are optional.
    """

    schema_version: int = 1
    source_id: str
    name: str
    cooking_time_min: int | None = None
    servings: int | None = None
    instructions_md: str | None = None
    image_url: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    nutrition_json: dict | None = None
    tags: list[str] = []
    ingredients: list[RecipeIngredientRecord]

    @field_validator("rating")
    @classmethod
    def _rating_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 5.0):
            raise ValueError("rating must be between 0 and 5")
        return v

    @field_validator("source_id", "name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v
