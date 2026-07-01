-- "category" becomes multi-select: a specimen can belong to several categories
-- (capped at 3 in the app). The single specimens.category column is KEPT as the
-- PRIMARY category (the first one chosen) -- it still backs the generated
-- thumbnail motif, the profile row, and back-compat reads. The full set lives in
-- a join table (mirroring the tags pattern), which is what filtering and facets
-- read from so a specimen matches on ANY of its categories.
--
-- Idempotent: CREATE ... IF NOT EXISTS + INSERT OR IGNORE backfill, so
-- re-running is safe.
--
-- Apply: wrangler d1 migrations apply embody --remote (or --local for dev).

CREATE TABLE IF NOT EXISTS specimen_categories (
  specimen_id TEXT NOT NULL REFERENCES specimens(id),
  category    TEXT NOT NULL,
  PRIMARY KEY (specimen_id, category)
);

-- Filtering does: EXISTS (SELECT 1 FROM specimen_categories WHERE
-- specimen_id = s.id AND category = ?). Index the category side so the facet
-- filter and the DISTINCT-facet read stay cheap at scale.
CREATE INDEX IF NOT EXISTS idx_specimen_categories_cat ON specimen_categories(category);

-- Backfill: seed the join table with each public/existing specimen's current
-- primary category. The primary is intentionally also present in the join table
-- so the membership filter is uniform (it matches the primary too).
INSERT OR IGNORE INTO specimen_categories (specimen_id, category)
SELECT id, category
FROM specimens
WHERE category IS NOT NULL AND category <> '';
