CREATE TABLE IF NOT EXISTS meal_plan_favorites (
    plan_id    INTEGER PRIMARY KEY REFERENCES meal_plans(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_meal_plans_week_draft
    ON meal_plans (week_of) WHERE status = 'draft';
