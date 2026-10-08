-- 0015_result_panel_scale.sql
-- The signed-in user's size for the specimen page's rendered-result panel, as a
-- multiple of its default size (1 = the default; the panel keeps the artwork's
-- aspect ratio at the same area as a 16:9 cover). Null = never resized. Set via
-- POST /api/account/preferences; anonymous visitors keep theirs in localStorage.
-- Apply (prod): wrangler d1 migrations apply embody --remote   (run from platform/)
-- Append-only migration.

ALTER TABLE users_profile ADD COLUMN result_panel_scale REAL;
