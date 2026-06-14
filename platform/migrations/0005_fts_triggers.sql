-- FTS integrity: orphaned specimens_fts rows on specimen delete. The mirror in
-- 0001_init.sql is hand-synced on INSERT only (app-side, via syncSpecimensFts),
-- so deleting a specimen left its FTS row behind -> stale search hits. ASCII only.
-- Apply (local): npx wrangler d1 execute embody --local --file=migrations/0005_fts_triggers.sql
-- (run from platform/, where this migrations/ dir lives). Append-only migration.
--
-- WHY a recreate, not just a trigger: specimens_fts was created with content=''
-- (a contentless FTS5 table). A plain contentless table supports NEITHER a
-- DELETE statement NOR a reliable 'delete' command from a trigger -- the
-- 'delete' command needs the ORIGINAL indexed column values (tags, dat_text,
-- author_handle), and an AFTER DELETE trigger only sees the specimens row
-- (OLD.*), which does NOT carry those. dat_text in particular lives outside D1
-- entirely (the R2 TDN blob), so no SQL trigger can reconstruct it.
--
-- The version-safe fix is to recreate the mirror as a CONTENTLESS-DELETE table
-- (content='', contentless_delete=1; SQLite 3.43.0+, which D1 is well past).
-- Contentless-delete tables DO support a plain `DELETE FROM fts WHERE rowid=?`,
-- so an AFTER DELETE trigger can remove the row by rowid alone -- no original
-- column values required. This matches the "drop -> recreate around D1 export"
-- note already in 0001 (virtual tables are not captured by D1 export anyway).
--
-- INSERT/UPDATE are still maintained app-side (syncSpecimensFts, an idempotent
-- INSERT OR REPLACE) because tags + dat_text are assembled outside the specimens
-- row and a pure SQL trigger cannot repopulate them. This migration owns only
-- the DELETE half (which SQL can do) plus the table-option upgrade that makes it
-- possible. See the comment on syncSpecimensFts in apps/web/src/server/db.ts.
--
-- TESTING NOTE -- a trigger that writes to an FTS5 virtual table only runs when
-- the connection has trusted_schema ON (SQLite's default, which D1 and the
-- wrangler --local workerd engine both use; FTS5-maintenance triggers are the
-- documented D1 pattern). A hardened sqlite3 build with trusted_schema=OFF
-- (notably the macOS system `sqlite3` CLI) will instead throw
-- "unsafe use of virtual table specimens_fts" on `DELETE FROM specimens`. That
-- is a CLI artifact, NOT a D1 problem -- verify deletes with
-- `wrangler d1 execute embody --local`, not the system sqlite3.

-- 1. Drop the old contentless mirror (and any prior triggers, defensively).
DROP TRIGGER IF EXISTS specimens_fts_ad;
DROP TABLE IF EXISTS specimens_fts;

-- 2. Recreate it as contentless-delete so DELETE-by-rowid is supported. Same
--    columns/order as 0001 so the app-side upsert and every MATCH query are
--    unchanged.
CREATE VIRTUAL TABLE specimens_fts USING fts5(
  slug UNINDEXED, title, description, tags, author_handle, dat_text,
  content='', contentless_delete=1
);

-- 3. Repopulate from the D1 rows that survive the recreate. dat_text is sourced
--    from the R2 TDN blob app-side, so it cannot be rebuilt in SQL here -- it is
--    seeded empty and re-filled on the next submit/edit (syncSpecimensFts). tags
--    ARE in D1, so they are restored via a correlated GROUP_CONCAT. rowid must
--    match specimens.rowid (the join key every MATCH query uses).
INSERT INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)
SELECT
  s.rowid,
  s.slug,
  s.title,
  s.description,
  COALESCE(
    (SELECT GROUP_CONCAT(t.name, ' ')
       FROM specimen_tags st
       JOIN tags t ON t.id = st.tag_id
      WHERE st.specimen_id = s.id),
    ''
  ),
  u.handle,
  ''
FROM specimens s
JOIN users_profile u ON u.id = s.author_id;

-- 4. The DELETE trigger: when a specimen row is removed, delete its FTS mirror
--    by rowid. Allowed because the table is now contentless_delete=1.
CREATE TRIGGER specimens_fts_ad AFTER DELETE ON specimens BEGIN
  DELETE FROM specimens_fts WHERE rowid = OLD.rowid;
END;
