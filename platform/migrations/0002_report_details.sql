-- Free-text note attached to a moderation report. Used by the "other" reason so
-- the reporter can explain in their own words; optional for the fixed reasons.
-- Nullable -- existing reports keep NULL.
ALTER TABLE reports ADD COLUMN details TEXT;
