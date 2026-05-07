"""FastMCP server exposing pantry-cooking-vibes tools to Claude Code (stdio transport)."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from pantry_cooking_vibes.mcp_server import tools

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Tools to plan meals from a local recipe database. Search recipes, manage "
    "the pantry, build weekly meal plans, and derive a shopping list. "
    "compute_shopping_list is currently QUALITATIVE: it lists which canonical "
    "ingredients are needed vs. covered by the pantry, but does not compare "
    "quantities (recipe quantities are not yet parsed)."
)


def build_server() -> FastMCP:
    mcp = FastMCP("pantry-cooking-vibes", instructions=INSTRUCTIONS)

    @mcp.tool()
    def search_recipes(
        query: str = "",
        max_time_min: int | None = None,
        tags: list[str] | None = None,
        ingredients: list[str] | None = None,
        ingredient_mode: str = "and",
        pantry_only: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        """Search recipes via FTS5 plus cooking-time, tag, and ingredient filters.

        Args:
            query: Full-text search string (matches name + instructions). Empty = browse all.
            max_time_min: Only return recipes with cooking_time_min <= this.
            tags: Recipes must have ALL these tags (case-insensitive).
            ingredients: Canonical ingredient names the recipe must contain.
            ingredient_mode: 'and' (recipe must contain all) or 'or' (any).
            pantry_only: True = only recipes whose mapped ingredients are all in the pantry.
            limit: Max results, default 20, capped at 100.
        """
        return tools.search_recipes(
            query=query,
            max_time_min=max_time_min,
            tags=tags,
            ingredients=ingredients,
            ingredient_mode=ingredient_mode,
            pantry_only=pantry_only,
            limit=limit,
        )

    @mcp.tool()
    def get_recipe(recipe_id: int) -> dict | None:
        """Fetch a recipe by id with its full ingredient list (canonical names) and tags."""
        return tools.get_recipe(recipe_id)

    @mcp.tool()
    def list_pantry() -> list[dict]:
        """List everything currently in the pantry (joined with canonical names)."""
        return tools.list_pantry()

    @mcp.tool()
    def add_pantry_item(
        canonical_id: int,
        quantity: float,
        unit: str | None = None,
        expires_at: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Insert a pantry row. To adjust an existing row, remove and re-add."""
        return tools.add_pantry_item(canonical_id, quantity, unit, expires_at, note)

    @mcp.tool()
    def remove_pantry_item(item_id: int) -> dict:
        """Delete a pantry row by id."""
        return tools.remove_pantry_item(item_id)

    @mcp.tool()
    def find_canonical_ingredient(query: str, limit: int = 10) -> list[dict]:
        """Find canonical ingredients by partial name/alias match.

        Use before add_pantry_item to translate an English ingredient name
        ("broccoli") to a canonical_id.
        """
        return tools.find_canonical_ingredient(query, limit)

    @mcp.tool()
    def create_meal_plan(week_of: str, notes: str | None = None) -> dict:
        """Create an empty meal plan.

        Args:
            week_of: ISO date of the week start, YYYY-MM-DD.
            notes: Free-form notes (often the original planning prompt).
        """
        return tools.create_meal_plan(week_of, notes)

    @mcp.tool()
    def add_recipe_to_plan(
        plan_id: int,
        recipe_id: int,
        day: str | None = None,
        meal_slot: str | None = None,
        servings_planned: int = 1,
    ) -> dict:
        """Add a recipe to a meal plan.

        Args:
            day: 'mon'..'sun' or None.
            meal_slot: 'breakfast', 'lunch', 'dinner', or None.
            servings_planned: Default 1, must be >= 1.
        """
        return tools.add_recipe_to_plan(plan_id, recipe_id, day, meal_slot, servings_planned)

    @mcp.tool()
    def remove_meal_plan_item(item_id: int) -> dict:
        """Delete a meal_plan_item by id (use to swap recipes within a plan)."""
        return tools.remove_meal_plan_item(item_id)

    @mcp.tool()
    def list_meal_plans() -> list[dict]:
        """List all meal plans newest-first with item counts."""
        return tools.list_meal_plans()

    @mcp.tool()
    def get_meal_plan(plan_id: int) -> dict | None:
        """Fetch a meal plan with all items and recipe names."""
        return tools.get_meal_plan(plan_id)

    @mcp.tool()
    def compute_shopping_list(plan_id: int) -> dict:
        """Derive a shopping list for a meal plan.

        v1 is QUALITATIVE: returns canonical ingredients you need to buy vs.
        which ones the pantry already covers, plus any recipe ingredients that
        haven't been mapped to a canonical (uncategorized). Does not compare
        quantities -- recipe quantities are not parsed yet.
        """
        return tools.compute_shopping_list(plan_id)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_server().run()


if __name__ == "__main__":
    main()
