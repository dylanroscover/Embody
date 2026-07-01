-- Optional, free-form display name on the app-domain user profile. Rendered as
-- the primary identity ("Dylan Roscover") with the @handle as a secondary line;
-- when null the UI falls back to "@handle" alone. The handle stays the unique,
-- URL-safe key (users_profile.handle UNIQUE); display_name is non-unique and may
-- be empty. Both are user-editable via /api/account/profile.
--
-- Apply: wrangler d1 migrations apply embody (or --local for the dev miniflare).

ALTER TABLE users_profile ADD COLUMN display_name TEXT;
