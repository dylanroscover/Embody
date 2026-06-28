-- Rename the specimens.difficulty column to "level". This is a UI/voice rename
-- only -- the values are unchanged (starter | intermediate | advanced). The app
-- now calls this facet "level" everywhere (contract type Level, SUBMIT_LEVELS,
-- form field name=level, query param ?level=, collection facet, card badge).
--
-- Apply: wrangler d1 migrations apply embody --remote (or --local for dev).
-- NOTE: this is a column RENAME, so old code expects "difficulty" and new code
-- expects "level" -- apply the migration and deploy the matching code together.

ALTER TABLE specimens RENAME COLUMN difficulty TO level;

-- Reindex under the new name. Modern SQLite auto-rewrites the old index to follow
-- the column rename, but recreate it cleanly so the index name matches too.
DROP INDEX IF EXISTS idx_specimens_difficulty;
CREATE INDEX IF NOT EXISTS idx_specimens_level ON specimens(level);
