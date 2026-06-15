-- Emoji reactions: replace the binary `like` with multi-emoji reactions that
-- stack per emoji. `reactions` is the (user, specimen, emoji) source-of-truth
-- row set; `specimens.reactions_summary` is a denormalized JSON map
-- {emoji: count} read directly by the cards and detail page (zero extra queries),
-- and `specimens.likes_count` is repurposed as the denormalized TOTAL reaction
-- count (sum across emojis) so existing sorts (popular, FTS tiebreak) keep working.
-- Both denormalized values are recomputed from `reactions` on every toggle.
--
-- ASCII only: the thumbs-up backfill glyph (U+1F44D) is built with char(128077)
-- so this source file stays pure ASCII (see .claude/rules/ascii-punctuation.md).
-- Apply: wrangler d1 migrations apply <DB> (or --file for the local miniflare).

CREATE TABLE IF NOT EXISTS reactions (
  user_id     TEXT NOT NULL REFERENCES users_profile(id),
  specimen_id TEXT NOT NULL REFERENCES specimens(id),
  emoji       TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, specimen_id, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reactions_specimen ON reactions(specimen_id);

ALTER TABLE specimens ADD COLUMN reactions_summary TEXT NOT NULL DEFAULT '{}';

-- Carry forward any existing likes as a thumbs-up reaction so no engagement is
-- lost. The legacy `likes` table is left in place (read-only) but no longer written.
INSERT OR IGNORE INTO reactions (user_id, specimen_id, emoji, created_at)
  SELECT user_id, specimen_id, char(128077), created_at FROM likes;

-- Seed the denormalized summary for specimens that had likes. After backfill the
-- only reaction is the thumbs-up, so the map is {"<thumbs-up>": likes_count}.
UPDATE specimens
  SET reactions_summary = '{"' || char(128077) || '":' || likes_count || '}'
  WHERE likes_count > 0;
