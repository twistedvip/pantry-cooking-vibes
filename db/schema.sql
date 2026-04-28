-- Meal Planner SQLite Schema
-- Idempotent: safe to re-run against an existing database.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- canonical_ingredients
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS canonical_ingredients (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,  -- 'broccoli', 'chicken breast'
    category     TEXT,                     -- 'vegetable','protein','grain','dairy',...
    default_unit TEXT,                     -- 'lb','oz','count','cup'
    aliases      TEXT    DEFAULT '[]'      -- JSON array: ['broccolini','broccoli florets']
);

CREATE INDEX IF NOT EXISTS idx_canonical_ingredients_name
    ON canonical_ingredients (name);

-- ---------------------------------------------------------------------------
-- recipes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recipes (
    id               INTEGER PRIMARY KEY,
    source           TEXT    NOT NULL CHECK (source IN ('hungryroot','url','manual')),
    source_id        TEXT,              -- HR pairing id, original URL, or NULL
    name             TEXT    NOT NULL,
    cooking_time_min INTEGER CHECK (cooking_time_min IS NULL OR cooking_time_min >= 0),
    servings         INTEGER CHECK (servings IS NULL OR servings >= 1),
    instructions_md  TEXT,
    -- Compact macro dict: {calories, protein_g, fat_g, carbs_g, fiber_g, sodium_mg}
    nutrition_json   TEXT    CHECK (nutrition_json IS NULL OR json_valid(nutrition_json)),
    image_url        TEXT,
    rating           REAL    CHECK (rating IS NULL OR (rating >= 0 AND rating <= 5)),
    rating_count     INTEGER CHECK (rating_count IS NULL OR rating_count >= 0),
    imported_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_recipes_source
    ON recipes (source);

CREATE INDEX IF NOT EXISTS idx_recipes_rating
    ON recipes (rating);

-- ---------------------------------------------------------------------------
-- Full-text search for recipes (FTS5)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts
    USING fts5 (
        name,
        instructions_md,
        content     = 'recipes',
        content_rowid = 'id'
    );

-- Keep FTS index in sync with recipes rows
CREATE TRIGGER IF NOT EXISTS recipes_ai AFTER INSERT ON recipes BEGIN
    INSERT INTO recipes_fts (rowid, name, instructions_md)
    VALUES (new.id, new.name, new.instructions_md);
END;

CREATE TRIGGER IF NOT EXISTS recipes_ad AFTER DELETE ON recipes BEGIN
    INSERT INTO recipes_fts (recipes_fts, rowid, name, instructions_md)
    VALUES ('delete', old.id, old.name, old.instructions_md);
END;

CREATE TRIGGER IF NOT EXISTS recipes_au AFTER UPDATE OF name, instructions_md ON recipes BEGIN
    INSERT INTO recipes_fts (recipes_fts, rowid, name, instructions_md)
    VALUES ('delete', old.id, old.name, old.instructions_md);
    INSERT INTO recipes_fts (rowid, name, instructions_md)
    VALUES (new.id, new.name, new.instructions_md);
END;

-- ---------------------------------------------------------------------------
-- recipe_ingredients
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id            INTEGER PRIMARY KEY,
    recipe_id     INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    canonical_id  INTEGER REFERENCES canonical_ingredients (id) ON DELETE SET NULL,
    original_text TEXT,   -- raw source string, e.g. 'Broccoli Florets, 1 bag'
    quantity      REAL,   -- best-effort parsed amount
    unit          TEXT,
    notes         TEXT    -- 'chopped', 'optional', etc.
);

CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe_id
    ON recipe_ingredients (recipe_id);

CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_canonical_id
    ON recipe_ingredients (canonical_id);

-- ---------------------------------------------------------------------------
-- recipe_tags
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recipe_tags (
    recipe_id INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    tag       TEXT    NOT NULL,  -- 'mexican', 'gluten-free', 'quick'
    PRIMARY KEY (recipe_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_recipe_tags_tag
    ON recipe_tags (tag);

-- ---------------------------------------------------------------------------
-- pantry
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pantry (
    id           INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES canonical_ingredients (id) ON DELETE RESTRICT,
    quantity     REAL    NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    unit         TEXT,
    added_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_pantry_canonical_id
    ON pantry (canonical_id);

-- ---------------------------------------------------------------------------
-- meal_plans
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meal_plans (
    id         INTEGER PRIMARY KEY,
    week_of    TEXT    NOT NULL,  -- ISO week start date e.g. '2026-04-13'
    status     TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed')),
    notes      TEXT,              -- free-form prompt that produced this plan
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_meal_plans_week_of
    ON meal_plans (week_of);

-- ---------------------------------------------------------------------------
-- meal_plan_items
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meal_plan_items (
    id               INTEGER PRIMARY KEY,
    plan_id          INTEGER NOT NULL REFERENCES meal_plans (id) ON DELETE CASCADE,
    recipe_id        INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    day              TEXT    CHECK (day IS NULL OR day IN ('mon','tue','wed','thu','fri','sat','sun')),
    meal_slot        TEXT    CHECK (meal_slot IS NULL OR meal_slot IN ('breakfast','lunch','dinner')),
    servings_planned INTEGER NOT NULL DEFAULT 1 CHECK (servings_planned >= 1)
);

CREATE INDEX IF NOT EXISTS idx_meal_plan_items_plan_id
    ON meal_plan_items (plan_id);

CREATE INDEX IF NOT EXISTS idx_meal_plan_items_recipe_id
    ON meal_plan_items (recipe_id);

-- ---------------------------------------------------------------------------
-- shopping_list_items
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shopping_list_items (
    id              INTEGER PRIMARY KEY,
    plan_id         INTEGER NOT NULL REFERENCES meal_plans (id) ON DELETE CASCADE,
    canonical_id    INTEGER NOT NULL REFERENCES canonical_ingredients (id) ON DELETE RESTRICT,
    quantity_needed REAL    NOT NULL DEFAULT 0 CHECK (quantity_needed >= 0),
    unit            TEXT,
    reason          TEXT  -- 'for recipe X' / 'buffer'
);

CREATE INDEX IF NOT EXISTS idx_shopping_list_items_plan_id
    ON shopping_list_items (plan_id);

CREATE INDEX IF NOT EXISTS idx_shopping_list_items_canonical_id
    ON shopping_list_items (canonical_id);

-- ---------------------------------------------------------------------------
-- ingredient_mapping_queue  (Claude-assisted normalization review)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingredient_mapping_queue (
    id                   INTEGER PRIMARY KEY,
    source               TEXT    NOT NULL,  -- 'hungryroot_product' | 'url_import'
    source_key           TEXT    NOT NULL,  -- HR product slug/id or raw ingredient string
    original_text        TEXT    NOT NULL,
    proposed_canonical_id INTEGER REFERENCES canonical_ingredients (id) ON DELETE SET NULL,
    confidence           REAL,
    status               TEXT    NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected')),
    UNIQUE (source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_ingredient_mapping_queue_status
    ON ingredient_mapping_queue (status);

CREATE INDEX IF NOT EXISTS idx_ingredient_mapping_queue_source_key
    ON ingredient_mapping_queue (source, source_key);
