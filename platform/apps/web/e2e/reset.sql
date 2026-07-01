-- e2e test-isolation reset (local D1 only). Full wipe of all app + auth data so
-- write-tests (which create e2e-* specimens and e2e users) cannot leak into
-- read-tests (notably the homepage's "3 newest specimens" assertion) or the
-- admin sign-in flow. Run by e2e/global.setup.ts BEFORE the suite, immediately
-- followed by src/server/seed.sql, which rebuilds the canonical seed from
-- scratch (dev-user + the six first-party specimens + their categories/FTS).
-- ASCII only.
--
-- Order matters: D1 enforces foreign keys and ignores PRAGMA foreign_keys=OFF,
-- so every child table is cleared before its parent. Tables referencing
-- specimens(id): specimen_versions, specimen_tags, scans(via version), likes,
-- comments, reports, reactions, specimen_categories. Referencing
-- users_profile(id): specimens, likes, comments, reports, reactions. Referencing
-- user(id): session, account (ON DELETE CASCADE).

-- FTS self-heal: a prior seed.sql may have recreated specimens_fts WITHOUT
-- contentless_delete=1, which breaks the specimens_fts_ad delete trigger
-- (`DELETE FROM specimens_fts` is illegal on a plain contentless table) and would
-- make the `DELETE FROM specimens` below fail. Recreate the mirror correctly and
-- the trigger before bulk-deleting. seed.sql rebuilds/repopulates it afterward.
DROP TRIGGER IF EXISTS specimens_fts_ad;
DROP TABLE IF EXISTS specimens_fts;
CREATE VIRTUAL TABLE specimens_fts USING fts5(
  slug UNINDEXED, title, description, tags, author_handle, dat_text,
  content='', contentless_delete=1
);
CREATE TRIGGER specimens_fts_ad AFTER DELETE ON specimens BEGIN
  DELETE FROM specimens_fts WHERE rowid = OLD.rowid;
END;

-- Engagement + denormalized join tables (children of specimens AND users_profile).
DELETE FROM reactions;
DELETE FROM reports;
DELETE FROM comments;
DELETE FROM likes;
DELETE FROM specimen_categories;

-- Specimen sub-rows, then specimens.
DELETE FROM scans;
DELETE FROM specimen_tags;
DELETE FROM specimen_versions;
DELETE FROM specimens;

-- Better Auth state (session/account cascade from user, but be explicit).
DELETE FROM session;
DELETE FROM account;
DELETE FROM verification;
DELETE FROM user;

-- All profiles (seed.sql recreates 'dev-user') and the audit log.
DELETE FROM users_profile;
DELETE FROM audit_log;
