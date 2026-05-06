-- 004_drop_recipes_without_image.sql
--
-- Purge recipes that landed without an image_url. Going forward, ingest
-- (jsonl_ingest + url_import) skips image-less records, but historical
-- imports (notably hungryroot) wrote rows whose images never loaded; this
-- migration brings the DB in line with the new invariant.
--
-- Cascading cleanup is handled by ON DELETE CASCADE on recipe_ingredients,
-- recipe_tags, meal_plan_items, and recipe_favorites. The recipes_ad trigger
-- keeps recipes_fts in sync row-by-row.

DELETE FROM recipes
 WHERE image_url IS NULL
    OR trim(image_url) = '';
