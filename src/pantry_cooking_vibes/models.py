from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

# Free-form source names registered by importers (core or plugin).
# Lowercase ASCII letter to start, then lowercase letters / digits / hyphens.
SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class CanonicalIngredient(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    name: str
    category: str | None = None
    default_unit: str | None = None
    aliases: list[str] = []

    @field_validator("aliases", mode="before")
    @classmethod
    def parse_aliases(cls, v: object) -> list[str]:
        if isinstance(v, str):
            import json

            return json.loads(v)
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        raise TypeError(f"aliases must be a JSON string, list, or None; got {type(v).__name__}")


class RecipeIngredient(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    recipe_id: int
    canonical_id: int | None = None
    original_text: str | None = None
    quantity: float | None = None
    unit: str | None = None
    notes: str | None = None


class Recipe(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    source: str
    source_id: str | None = None
    name: str
    cooking_time_min: int | None = None
    servings: int | None = None
    instructions_md: str | None = None
    nutrition_json: str | None = None
    image_url: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    imported_at: str | None = None
    tags: list[str] = []
    ingredients: list[RecipeIngredient] = []

    @field_validator("source")
    @classmethod
    def validate_source_name(cls, v: str) -> str:
        if not SOURCE_NAME_RE.fullmatch(v):
            raise ValueError(
                "source must be lowercase letters, digits, or hyphens "
                "and start with a letter (e.g. 'manual')"
            )
        return v

    @field_validator("rating")
    @classmethod
    def rating_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 5.0):
            raise ValueError("rating must be between 0 and 5")
        return v


class PantryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    canonical_id: int
    quantity: float = 0.0
    unit: str | None = None
    added_at: str | None = None
    expires_at: str | None = None
    note: str | None = None


class MealPlanItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    plan_id: int
    recipe_id: int
    day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None
    meal_slot: Literal["breakfast", "lunch", "dinner"] | None = None
    servings_planned: int = 1

    @field_validator("servings_planned")
    @classmethod
    def positive_servings(cls, v: int) -> int:
        if v < 1:
            raise ValueError("servings_planned must be >= 1")
        return v


class MealPlan(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    week_of: str  # ISO date string e.g. '2026-04-13'
    status: Literal["draft", "confirmed"] = "draft"
    notes: str | None = None
    created_at: str | None = None
    items: list[MealPlanItem] = []

    @field_validator("week_of")
    @classmethod
    def validate_week_of(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("week_of must be an ISO date string (YYYY-MM-DD)")
        return v


class ShoppingListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    plan_id: int
    canonical_id: int
    quantity_needed: float = 0.0
    unit: str | None = None
    reason: str | None = None


class IngredientMappingQueueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    source: str
    source_key: str
    original_text: str
    proposed_canonical_id: int | None = None
    confidence: float | None = None
    status: Literal["proposed", "approved", "rejected"] = "proposed"


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
