CREATE TABLE IF NOT EXISTS recipe_favorites (
    recipe_id INTEGER PRIMARY KEY,
    favorited_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);
