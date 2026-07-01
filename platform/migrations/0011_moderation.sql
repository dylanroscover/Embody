-- 0011_moderation.sql
-- Superadmin moderation: account ban/suspend + an app-wide audit log.
--
-- Ban model: a banned account is hidden everywhere (getSessionUser returns null,
-- so they are treated as logged-out and cannot sign in or submit) and their
-- specimens drop out of every PUBLIC query via an `author NOT banned` filter --
-- fully reversible (unban -> content reappears), no per-specimen bookkeeping.
--
-- audit_log: the queryable record of security / abuse / admin events (deletes,
-- bans, moderation, report filings, auto-actions). Written via logEvent() in
-- db.ts; surfaced at /admin/activity. High-volume app telemetry stays in
-- structured console logs (Cloudflare Workers Logs / Analytics Engine), NOT here.

ALTER TABLE users_profile ADD COLUMN banned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users_profile ADD COLUMN banned_reason TEXT;
ALTER TABLE users_profile ADD COLUMN banned_at TEXT;

-- Partial-style index for the common "is this author banned" filter on hot
-- public read paths (a plain index; SQLite picks it for `banned = 0/1`).
CREATE INDEX IF NOT EXISTS idx_users_profile_banned ON users_profile(banned);

CREATE TABLE IF NOT EXISTS audit_log (
  id           TEXT PRIMARY KEY,
  ts           TEXT NOT NULL DEFAULT (datetime('now')),
  actor_id     TEXT,            -- user id who performed the action (NULL = system/anon)
  actor_handle TEXT,            -- denormalized for display without a join
  action       TEXT NOT NULL,   -- dotted verb, e.g. user.ban | user.delete | specimen.delete | report.create
  target_type  TEXT,            -- user | specimen | report | ...
  target_id    TEXT,
  metadata     TEXT,            -- JSON blob of action-specific detail
  ip           TEXT             -- best-effort client IP (CF-Connecting-IP)
);

CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log(actor_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts DESC);
