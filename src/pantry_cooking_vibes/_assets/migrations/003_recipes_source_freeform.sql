-- Drop the recipes.source CHECK constraint so importers can register
-- arbitrary source names (e.g. plugin-provided sites). Source naming is now
-- validated at the application layer (lowercase, [a-z0-9-]).
-- SQLite cannot ALTER a CHECK constraint, so the table must be rebuilt.
-- foreign_keys is disabled for the rebuild so dropping `recipes` does not
-- cascade-delete recipe_ingredients / recipe_tags / meal_plan_items / recipe_favorites.

PRAGMA foreign_keys = OFF;

DROP TRIGGER IF EXISTS recipes_ai;
DROP TRIGGER IF EXISTS recipes_ad;
DROP TRIGGER IF EXISTS recipes_au;

DROP TABLE IF EXISTS recipes_new;
CREATE TABLE recipes_new (
    id               INTEGER PRIMARY KEY,
    source           TEXT    NOT NULL,
    source_id        TEXT,
    name             TEXT    NOT NULL,
    cooking_time_min INTEGER CHECK (cooking_time_min IS NULL OR cooking_time_min >= 0),
    servings         INTEGER CHECK (servings IS NULL OR servings >= 1),
    instructions_md  TEXT,
    nutrition_json   TEXT    CHECK (nutrition_json IS NULL OR json_valid(nutrition_json)),
    image_url        TEXT,
    rating           REAL    CHECK (rating IS NULL OR (rating >= 0 AND rating <= 5)),
    rating_count     INTEGER CHECK (rating_count IS NULL OR rating_count >= 0),
    imported_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_id)
);

INSERT INTO recipes_new
    (id, source, source_id, name, cooking_time_min, servings,
     instructions_md, nutrition_json, image_url, rating, rating_count, imported_at)
SELECT id, source, source_id, name, cooking_time_min, servings,
       instructions_md, nutrition_json, image_url, rating, rating_count, imported_at
FROM recipes;

DROP TABLE recipes;
ALTER TABLE recipes_new RENAME TO recipes;

CREATE INDEX IF NOT EXISTS idx_recipes_source ON recipes (source);
CREATE INDEX IF NOT EXISTS idx_recipes_rating ON recipes (rating);

CREATE TRIGGER recipes_ai AFTER INSERT ON recipes BEGIN
    INSERT INTO recipes_fts (rowid, name, instructions_md)
    VALUES (new.id, new.name, new.instructions_md);
END;
CREATE TRIGGER recipes_ad AFTER DELETE ON recipes BEGIN
    INSERT INTO recipes_fts (recipes_fts, rowid, name, instructions_md)
    VALUES ('delete', old.id, old.name, old.instructions_md);
END;
CREATE TRIGGER recipes_au AFTER UPDATE OF name, instructions_md ON recipes BEGIN
    INSERT INTO recipes_fts (recipes_fts, rowid, name, instructions_md)
    VALUES ('delete', old.id, old.name, old.instructions_md);
    INSERT INTO recipes_fts (rowid, name, instructions_md)
    VALUES (new.id, new.name, new.instructions_md);
END;

-- Refresh the FTS contentless index against the rebuilt content table.
INSERT INTO recipes_fts (recipes_fts) VALUES ('rebuild');

PRAGMA foreign_keys = ON;
