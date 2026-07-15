-- Repair legacy `requires` values.
--
-- Pre-0009 rows and the dev seed stored a scalar (e.g. 'none') instead of a JSON
-- array. That is invalid JSON, so json_each(requires) in the collection facet and
-- filter queries threw "malformed JSON: SQLITE_ERROR" -- crashing the whole
-- collection listing into its fixtures fallback (newly published specimens never
-- appeared, and authors read as the seed author).
--
-- Normalize ONLY rows whose `requires` is not valid JSON, and PRESERVE meaning:
--     NULL / '' / 'none'    -> '[]'        (no requirements)
--     a legacy scalar 'gpu' -> '["gpu"]'   (single requirement, NOT discarded)
-- Rows that already hold valid JSON are excluded by the WHERE and left untouched.
-- Idempotent: afterwards every row is valid JSON, so re-running is a no-op.
UPDATE specimens
SET requires = CASE
    WHEN requires IS NULL OR trim(requires) = '' OR lower(trim(requires)) = 'none' THEN '[]'
    ELSE json_array(requires)
  END
WHERE requires IS NULL OR NOT json_valid(requires);
