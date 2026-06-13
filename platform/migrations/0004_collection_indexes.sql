-- Collection-page scaling: indexes that make the public list/filter/sort/paginate
-- query O(page) at thousands of specimens. Each keyset sort resumes via a single
-- indexed range scan; facet filters resolve via equality lookups. ASCII only.
-- Apply (local): npx wrangler d1 execute embody --local --file=migrations/0004_collection_indexes.sql
-- (run from platform/, where this migrations/ dir lives; the binding's migrations_dir is ../../migrations)
-- Append-only migration; 0003 is owned by the auth agent.

-- Every collection query filters visibility='public', so each composite index
-- leads with visibility, then the sort key, then the slug tiebreaker -- matching
-- the ORDER BY (sort_key, slug) the keyset pagination emits.

-- "newest" sort: created_at DESC, slug ASC.
CREATE INDEX IF NOT EXISTS idx_specimens_visible_created
  ON specimens(visibility, created_at, slug);

-- "copied" sort: copies_count DESC, slug ASC.
CREATE INDEX IF NOT EXISTS idx_specimens_visible_copies
  ON specimens(visibility, copies_count, slug);

-- "az" sort: title (case-insensitive) ASC, slug ASC.
CREATE INDEX IF NOT EXISTS idx_specimens_visible_title
  ON specimens(visibility, title COLLATE NOCASE, slug);

-- Facet equality filters (category already has idx_specimens_category from 0001).
CREATE INDEX IF NOT EXISTS idx_specimens_difficulty
  ON specimens(difficulty);

CREATE INDEX IF NOT EXISTS idx_specimens_requires
  ON specimens(requires);
