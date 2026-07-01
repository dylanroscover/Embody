-- 0013_cover_video.sql
-- Add the nullable video_key to specimens. Video covers are purely additive:
-- every video cover ALSO carries a poster in thumbnail_key, so image-only covers
-- behave exactly as before. Poster stays at thumbnails/{sha256}; the video blob
-- lives at videos/{sha256} in the same embody-blobs R2 bucket. Null = no video.
-- Apply (prod): wrangler d1 migrations apply embody --remote   (run from platform/)
-- Append-only migration.

ALTER TABLE specimens ADD COLUMN video_key TEXT;
