ALTER TABLE canonical_ingredients ADD COLUMN freshness_days INTEGER;

UPDATE canonical_ingredients SET freshness_days = CASE category
    WHEN 'protein'   THEN 4
    WHEN 'vegetable' THEN 7
    WHEN 'fruit'     THEN 7
    WHEN 'dairy'     THEN 14
    WHEN 'grain'     THEN 180
    WHEN 'legume'    THEN 365
    WHEN 'nut'       THEN 180
    WHEN 'seed'      THEN 365
    WHEN 'fat'       THEN 30
    WHEN 'herb'      THEN 7
    WHEN 'spice'     THEN 365
    WHEN 'condiment' THEN 30
    WHEN 'baking'    THEN 365
    WHEN 'beverage'  THEN 7
    ELSE NULL
END;
