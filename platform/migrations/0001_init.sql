-- FROZEN CONTRACT C5 - D1 (SQLite) schema. Raw .tdn + thumbnail BYTES live in R2; D1 holds only
-- metadata + R2 keys. Better Auth creates its OWN user/session/account/verification tables via its
-- migrations; this file owns the APP-DOMAIN tables and references the Better Auth user id. ASCII only.
-- Apply: wrangler d1 migrations apply <DB>. Append-only; new changes go in 000N_*.sql.

-- App-side user profile (1:1 with the Better Auth user; trust_level is ours).
CREATE TABLE IF NOT EXISTS users_profile (
  id            TEXT PRIMARY KEY,              -- = Better Auth user id
  handle        TEXT UNIQUE NOT NULL,
  avatar_url    TEXT,
  bio           TEXT,
  trust_level   TEXT NOT NULL DEFAULT 'anon',  -- anon | verified | curator | admin
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS specimens (
  id              TEXT PRIMARY KEY,
  slug            TEXT UNIQUE NOT NULL,
  author_id       TEXT NOT NULL REFERENCES users_profile(id),
  title           TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  category        TEXT NOT NULL,
  difficulty      TEXT NOT NULL DEFAULT 'intermediate', -- starter|intermediate|advanced
  requires        TEXT NOT NULL DEFAULT 'none',
  op_count        INTEGER NOT NULL DEFAULT 0,
  family_summary  TEXT,                          -- denormalized e.g. "TOP,CHOP,DAT"
  current_version_id TEXT,
  thumbnail_key   TEXT,                          -- R2 key
  license         TEXT NOT NULL DEFAULT 'CC-BY-4.0',
  visibility      TEXT NOT NULL DEFAULT 'public', -- public|unlisted|private
  tier            TEXT NOT NULL DEFAULT 'community', -- community|verified|featured
  scan_status     TEXT NOT NULL DEFAULT 'pending',  -- pending|clean|flagged|blocked
  capability_json TEXT,                          -- denormalized C2 CapabilityJson of latest version
  likes_count     INTEGER NOT NULL DEFAULT 0,    -- denormalized from Durable Object
  views_count     INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_specimens_author   ON specimens(author_id);
CREATE INDEX IF NOT EXISTS idx_specimens_category ON specimens(category);
CREATE INDEX IF NOT EXISTS idx_specimens_tier     ON specimens(tier);

-- Immutable versions; every submit/edit is a new row (git-diff friendly). Blob is in R2 by sha256.
CREATE TABLE IF NOT EXISTS specimen_versions (
  id            TEXT PRIMARY KEY,
  specimen_id   TEXT NOT NULL REFERENCES specimens(id),
  version_num   INTEGER NOT NULL,
  tdn_r2_key    TEXT NOT NULL,                  -- content-addressed (= sha256)
  tdn_sha256    TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  op_count      INTEGER NOT NULL DEFAULT 0,
  scan_id       TEXT,
  signature_ref TEXT,                           -- Sigstore Rekor entry
  changelog     TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(specimen_id, version_num)
);

CREATE TABLE IF NOT EXISTS scans (
  id              TEXT PRIMARY KEY,
  version_id      TEXT NOT NULL REFERENCES specimen_versions(id),
  scanner_version TEXT NOT NULL,
  verdict         TEXT NOT NULL,                -- clean|flagged|blocked
  capability_json TEXT NOT NULL,                -- C2 CapabilityJson
  findings_json   TEXT NOT NULL,                -- C2 ScanFinding[]
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tags (
  id   TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS specimen_tags (
  specimen_id TEXT NOT NULL REFERENCES specimens(id),
  tag_id      TEXT NOT NULL REFERENCES tags(id),
  PRIMARY KEY (specimen_id, tag_id)
);

-- FTS5 keyword search mirror. NOTE: drop -> export -> recreate around D1 export (virtual tables
-- are not captured by export). Kept in sync via triggers below.
CREATE VIRTUAL TABLE IF NOT EXISTS specimens_fts USING fts5(
  slug UNINDEXED, title, description, tags, author_handle, dat_text,
  content=''
);

CREATE TABLE IF NOT EXISTS likes (
  user_id     TEXT NOT NULL REFERENCES users_profile(id),
  specimen_id TEXT NOT NULL REFERENCES specimens(id),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, specimen_id)
);

CREATE TABLE IF NOT EXISTS comments (
  id                TEXT PRIMARY KEY,
  specimen_id       TEXT NOT NULL REFERENCES specimens(id),
  author_id         TEXT NOT NULL REFERENCES users_profile(id),
  parent_comment_id TEXT REFERENCES comments(id),
  body              TEXT NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_specimen ON comments(specimen_id);

CREATE TABLE IF NOT EXISTS reports (
  id          TEXT PRIMARY KEY,
  specimen_id TEXT NOT NULL REFERENCES specimens(id),
  reporter_id TEXT NOT NULL REFERENCES users_profile(id),
  reason      TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',     -- open|reviewing|actioned|dismissed
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
