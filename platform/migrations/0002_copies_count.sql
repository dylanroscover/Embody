-- Add the copies_count counter to specimens. Incremented each time a visitor
-- copies a specimen's TDN envelope from the website. ASCII only.
-- Apply: wrangler d1 migrations apply <DB> (or --file for the local miniflare).
ALTER TABLE specimens ADD COLUMN copies_count INTEGER NOT NULL DEFAULT 0;
