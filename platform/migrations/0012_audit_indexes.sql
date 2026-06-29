-- 0012_audit_indexes.sql
-- Performance/scalability indexes surfaced by the web-app audit. Indexes only --
-- no data change -- so this is safe to apply at any time. Validate each with
-- EXPLAIN QUERY PLAN against the real query before relying on it.
-- Apply (prod): wrangler d1 migrations apply embody --remote   (run from platform/)
-- Append-only migration.

-- "liked" / legacy "popular" collection sort orders by likes_count but 0004 only
-- created visible_created / visible_copies / visible_title -- NOT a likes index.
-- Without this the sort is a full scan of public specimens + a filesort per page.
-- Leads with visibility (every collection query filters visibility='public'),
-- then the sort key, then the slug tiebreaker -- matching ORDER BY (likes_count, slug).
CREATE INDEX IF NOT EXISTS idx_specimens_visible_likes
  ON specimens(visibility, likes_count, slug);

-- The reports table (0001) has NO secondary indexes. Every new report runs
-- distinct-reporter COUNTs for auto-moderation, and the admin queue counts/lists
-- by status + created_at -- all full scans as the table grows.
-- Auto-moderation: COUNT(DISTINCT reporter_id) WHERE specimen_id=? AND status IN (...)
CREATE INDEX IF NOT EXISTS idx_reports_specimen_status
  ON reports(specimen_id, status, reporter_id);

-- Admin dashboard/list: WHERE status=? ORDER BY created_at DESC
CREATE INDEX IF NOT EXISTS idx_reports_status_created
  ON reports(status, created_at);
