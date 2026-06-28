-- "requires" becomes a multi-select list: a specimen can declare several
-- hardware/software requirements. Stored as a JSON array of strings in the same
-- TEXT column. An empty array ([]) means it runs on stock TouchDesigner.
--
-- Convert existing single-value rows to JSON arrays. Idempotent: rows already
-- holding a JSON array are left untouched, so re-running is safe.
--
-- Apply: wrangler d1 migrations apply embody --remote (or --local for dev).

UPDATE specimens
SET requires = CASE
  WHEN requires IS NULL OR requires = '' OR requires = 'none' THEN '[]'
  WHEN json_valid(requires) AND json_type(requires) = 'array' THEN requires
  ELSE json_array(requires)
END;

-- The old single-value index no longer helps: filtering is now
-- EXISTS (SELECT 1 FROM json_each(requires) WHERE value = ?). Drop it; the
-- candidate set is already narrowed by the other indexed facets.
DROP INDEX IF EXISTS idx_specimens_requires;
