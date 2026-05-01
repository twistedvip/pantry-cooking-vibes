# Web UI (FastAPI)

Read-mostly browse UI served by `uvicorn`. Only the pantry routes write to
the DB; everything else (recipes, plans, shopping) is read-only from the
user's perspective. Favorites are the one exception — they write to
`recipe_favorites` but still live under the otherwise-read-only recipes
surface.

## Routes

| Method | Path                           | Handler                              | Writes?       |
| ------ | ------------------------------ | ------------------------------------ | ------------- |
| GET    | `/`                            | `routes/home.py::home`               | no            |
| GET    | `/static/*`                    | `StaticFiles`                        | no            |
| GET    | `/recipes`                     | `recipes.py::list_recipes`           | no            |
| GET    | `/recipes/{id}`                | `recipes.py::recipe_detail`          | no            |
| POST   | `/recipes/{id}/favorite`       | `recipes.py::toggle_favorite`        | `recipe_favorites` |
| GET    | `/pantry`                      | `pantry.py::pantry_page`             | no            |
| POST   | `/pantry/add`                  | `pantry.py::pantry_add`              | `pantry`      |
| POST   | `/pantry/{id}/delete`          | `pantry.py::pantry_delete`           | `pantry`      |
| GET    | `/plans`                       | `plans.py::list_plans`               | no            |
| GET    | `/plans/{id}`                  | `plans.py::plan_detail`              | no            |
| GET    | `/plans/{id}/shopping`         | `plans.py::plan_shopping`            | no            |

Filter state on `/recipes` is carried in query strings (`q`, `max_time`,
`tags`, `limit`, `fav`). Blank numeric fields from the HTML form are
coerced in `_parse_optional_int` — FastAPI's `Optional[int]` rejects `""`
with a 422, so the route accepts `str` and parses it. Non-numeric values
still raise 422.

## App factory

```python
# web/app.py
def create_app(db_path: Optional[Path] = None) -> FastAPI:
    app = FastAPI(...)
    if db_path is not None:
        app.dependency_overrides[get_db_path] = lambda: resolved
    app.mount("/static", StaticFiles(...))
    app.include_router(home.router)
    app.include_router(recipes.router)
    app.include_router(pantry.router)
    app.include_router(plans.router)
    return app
```

`app_factory.py` reads `PANTRY_COOKING_VIBES_DB` from the environment and
builds the module-level `app` for uvicorn. The CLI `serve-web` command sets
that env var, so uvicorn's reloader subprocesses see the same DB.

## Templates

Jinja2 files under `web/templates/`. Every page extends `base.html`. Layout:

```
templates/
  base.html
  home.html
  recipes/
    list.html
    detail.html
  pantry/
    list.html
  plans/
    list.html
    detail.html
    shopping.html
```

`web/deps.py::render(request, template, context)` is the canonical way to
respond. It passes `request` into the context (required by `Jinja2Templates`)
so templates can access `request.url.path` etc. (used for "preserve filter"
redirects on favorite toggles).

## Call flow: toggling a favorite

This is the most interesting round-trip in the app — it exercises form
posts, redirect-with-filter-preservation, and the `tools.py` → DB layer.

1. User clicks the ★/☆ button on a recipe card. The form:

   ```html
   <form method="post" action="/recipes/{{ r.id }}/favorite">
     <input type="hidden" name="favorite" value="{{ '0' if r.is_favorite else '1' }}">
     <input type="hidden" name="redirect_to" value="{{ request.url.path }}?{{ request.url.query }}">
     <button type="submit">...</button>
   </form>
   ```

2. `routes/recipes.py::toggle_favorite` resolves `db_path` via `Depends`,
   calls `tools.set_recipe_favorite(recipe_id, want_fav, db_path=...)`.

3. `tools.set_recipe_favorite`:
   - Checks `recipes.id` exists; raises `ValueError` if not.
   - If favoriting: `INSERT INTO recipe_favorites (recipe_id) VALUES (?)
     ON CONFLICT DO NOTHING`.
   - If unfavoriting: `DELETE FROM recipe_favorites WHERE recipe_id = ?`.

4. Route catches `ValueError`, turns it into a 404; otherwise returns a
   303 redirect.

5. Redirect target: `redirect_to` if it starts with `/` (same-origin guard),
   else `/recipes/{id}`. This is why toggling a favorite from a filtered
   list (`/recipes?q=soup&fav=1`) lands back on that same filtered list
   rather than a bare detail page.

6. Subsequent `GET /recipes` joins `recipe_favorites` into the search query,
   so the star renders filled and `favorites_only` filtering works.

## Pantry flow quirks

`pantry_add` accepts `quantity: float = Form(...)`. Negative quantities
raise `ValueError` inside `tools.add_pantry_item` (defense in depth
beyond the schema check). The route catches it and redirects with
`?error=...` instead of 500'ing.

`pantry_delete` returns 303 in both success and not-found cases; the
flash message is encoded in the query string. This keeps the UI a simple
POST-redirect-GET without session state.

## Static assets

`web/static/style.css` is the entire stylesheet. There is no JS build
step. If you need interactivity, add it inline or as a small file — the
UI is intentionally "works with JS disabled."

## Error handling

- Not-found resources: raise `HTTPException(404)`, which FastAPI renders as
  a JSON body by default. If you want a prettier 404 page, add an
  `@app.exception_handler(HTTPException)`.
- Form validation errors (negative quantity, unknown item): the pantry
  routes redirect with `?error=...`; `pantry/list.html` surfaces that.
- Missing DB at startup: `serve-web` exits 1 with a helpful message before
  uvicorn starts.
- Stale DB (missing a migration): `serve-web` auto-applies pending
  migrations and prints what it ran.
