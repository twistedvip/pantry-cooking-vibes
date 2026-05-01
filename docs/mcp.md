# MCP server

`meal-cli serve-mcp` starts a FastMCP (stdio) server that exposes 13 tools
to Claude Code. The transport is `stdio`, so Claude Code launches it as a
subprocess and speaks newline-delimited JSON-RPC over the pipe.

## Wiring into Claude Code

Add to your MCP config (e.g. `.claude/mcp.json` or the global Claude Code
config):

```json
{
  "mcpServers": {
    "pantry-cooking-vibes": {
      "command": "meal-cli",
      "args": ["serve-mcp"]
    }
  }
}
```

Confirm Claude picked it up with `/mcp` inside a session.

## Implementation shape

- [`src/pantry_cooking_vibes/mcp_server/tools.py`](../src/pantry_cooking_vibes/mcp_server/tools.py)
  holds the pure functions. Each takes an optional `db_path=` and returns
  plain `dict` / `list[dict]`.
- [`src/pantry_cooking_vibes/mcp_server/server.py`](../src/pantry_cooking_vibes/mcp_server/server.py)
  wraps each in an `@mcp.tool()` decorator. The server module adds no
  business logic; it's a thin adapter that supplies docstrings and
  schemas to the MCP client. The FastMCP server name is
  `pantry-cooking-vibes`.

Because `tools.py` is framework-free, the FastAPI web routes call the
same functions. One SQL implementation, two consumers.

## Tool surface

### Recipes

| Tool              | Signature                                                                 | Purpose |
| ----------------- | ------------------------------------------------------------------------- | ------- |
| `search_recipes`  | `(query="", max_time_min=None, tags=None, limit=20) -> list[dict]`        | FTS5 + cooking-time + tags. Empty query browses all, ordered by `is_favorite DESC, rating DESC`. |
| `get_recipe`      | `(recipe_id: int) -> dict | None`                                         | Full recipe with ingredients (joined canonical names), tags, `is_favorite`. |

`set_recipe_favorite` is intentionally *not* exposed over MCP — Claude
shouldn't be toggling the user's favorites. It's used by the web route
only.

### Pantry

| Tool                      | Signature                                                                                          |
| ------------------------- | -------------------------------------------------------------------------------------------------- |
| `list_pantry`             | `() -> list[dict]` — joined with canonical name and category.                                      |
| `add_pantry_item`         | `(canonical_id, quantity, unit=None, expires_at=None, note=None) -> dict` — raises on quantity<0. |
| `remove_pantry_item`      | `(item_id: int) -> {"removed": bool, "id": int}`                                                  |
| `find_canonical_ingredient` | `(query: str, limit=10) -> list[dict]` — LIKE match on name + aliases.                          |

Typical flow: `find_canonical_ingredient("broccoli")` → pick `id` →
`add_pantry_item(canonical_id=id, quantity=1, unit="head")`.

### Meal plans

| Tool                    | Signature                                                                                        |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| `create_meal_plan`      | `(week_of: str, notes=None) -> dict` — `week_of` must be `YYYY-MM-DD`.                           |
| `add_recipe_to_plan`    | `(plan_id, recipe_id, day=None, meal_slot=None, servings_planned=1) -> dict`                     |
| `remove_meal_plan_item` | `(item_id) -> {"removed": bool, "id": int}`                                                      |
| `list_meal_plans`       | `() -> list[dict]` — newest-first with item counts.                                              |
| `get_meal_plan`         | `(plan_id) -> dict | None` — plan + items with recipe names / image URLs.                        |

Allowed `day`: `mon`..`sun`. Allowed `meal_slot`: `breakfast`, `lunch`,
`dinner`. Either can be `None`.

### Shopping

| Tool                    | Signature                                                                  |
| ----------------------- | -------------------------------------------------------------------------- |
| `compute_shopping_list` | `(plan_id) -> {"plan_id", "needed", "covered_by_pantry", "uncategorized"}` |

v1 is **qualitative** — the server's instructions string tells Claude so it
won't mislead users. Quantity math is blocked on parsing recipe ingredient
quantities/units, which isn't in the schema yet (see `BACKLOG.md`).

Shape of each entry:

```json
{
  "needed": [
    {"canonical_id": 17, "name": "tofu", "category": "protein",
     "in_recipes": ["Sesame Tofu Bowl"]}
  ],
  "covered_by_pantry": [
    {"canonical_id": 3, "name": "broccoli", "category": "vegetable",
     "in_recipes": ["Broccoli Stir Fry"]}
  ],
  "uncategorized": [
    {"recipe_id": 12, "recipe_name": "Broccoli Soup",
     "original_text": "4 cups vegetable stock"}
  ]
}
```

## Server instructions

The `INSTRUCTIONS` string on
`FastMCP("pantry-cooking-vibes", instructions=...)` is sent to the model
alongside the tool list. It currently flags the qualitative-only status of
`compute_shopping_list`, which is the one behavior Claude could otherwise
misrepresent to the user.

## Testing against MCP tools

Unit tests under `tests/test_mcp_tools.py` call the functions in
`tools.py` directly — no FastMCP involvement. That's intentional: the
contract we care about is the function signature + return shape, and
keeping tests framework-free makes them fast and resilient to MCP
library churn.
