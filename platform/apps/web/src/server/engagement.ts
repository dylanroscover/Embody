// Engagement domain: emoji reactions (toggle per emoji) and reports (create) for
// specimens.
//
// `reactions` (migrations/0006) is the (user_id, specimen_id, emoji) row set --
// the source of truth. Two denormalized columns on `specimens` are recomputed
// from it on every toggle so reads stay O(1): `reactions_summary` is a JSON map
// {emoji: count}, and `likes_count` is repurposed as the TOTAL across emojis
// (kept so the existing "popular" sort and FTS tiebreak keep working). `reports`
// is an append-only moderation queue keyed by reporter.
//
// Request/response types live HERE (not packages/contracts/api.ts -- that file
// is owned by another agent). Keep these shapes self-contained.

import { isReactionEmoji } from "../lib/reactions";

// --- Public request/response types ----------------------------------------

// POST /api/specimens/:slug/react response. `reacted` is the user's resulting
// state for `emoji` after the toggle; `reactions` is the specimen's full
// {emoji: count} map; `mine` is every emoji the user now has on this specimen;
// `total` is the sum across emojis.
export interface ReactionToggleResult {
  emoji: string;
  reacted: boolean;
  reactions: Record<string, number>;
  mine: string[];
  total: number;
}

// Accepted moderation report reasons. Free-form detail is intentionally not
// accepted from the public endpoint -- a bounded vocabulary keeps the queue
// triageable and avoids storing arbitrary user text.
export const REPORT_REASONS = [
  "malware",
  "spam",
  "copyright",
  "inappropriate",
  "broken",
  "other"
] as const;

export type ReportReason = (typeof REPORT_REASONS)[number];

// POST /api/specimens/:slug/report response.
export interface ReportResult {
  id: string;
  reason: ReportReason;
  status: string;
}

export function isReportReason(value: unknown): value is ReportReason {
  return typeof value === "string" && (REPORT_REASONS as readonly string[]).includes(value);
}

// --- Operations ------------------------------------------------------------

// Toggle a user's reaction with `emoji` on a specimen, then recompute the
// denormalized tallies from the source-of-truth row set so they can never drift.
// The caller validates `emoji` against the allow-list before calling. Returns the
// user's resulting state, the full per-emoji map, the user's emojis, and the total.
export async function toggleReaction(
  db: D1Database,
  specimenId: string,
  userId: string,
  emoji: string
): Promise<ReactionToggleResult> {
  const existing = await db
    .prepare(
      "SELECT 1 AS one FROM reactions WHERE user_id = ? AND specimen_id = ? AND emoji = ? LIMIT 1"
    )
    .bind(userId, specimenId, emoji)
    .first<{ one: number }>();

  const reacted = !existing;

  if (reacted) {
    await db
      .prepare(
        "INSERT OR IGNORE INTO reactions (user_id, specimen_id, emoji) VALUES (?, ?, ?)"
      )
      .bind(userId, specimenId, emoji)
      .run();
  } else {
    await db
      .prepare("DELETE FROM reactions WHERE user_id = ? AND specimen_id = ? AND emoji = ?")
      .bind(userId, specimenId, emoji)
      .run();
  }

  // Recompute from the row set (not blind +/- 1) so a no-op INSERT OR IGNORE or a
  // double-delete can never desync the denormalized columns.
  const summary = await readReactionSummary(db, specimenId);
  await db
    .prepare("UPDATE specimens SET likes_count = ?, reactions_summary = ? WHERE id = ?")
    .bind(summary.total, JSON.stringify(summary.reactions), specimenId)
    .run();

  const mine = await getUserReactions(db, specimenId, userId);

  return { emoji, reacted, reactions: summary.reactions, mine, total: summary.total };
}

// Aggregate the per-emoji tallies for a specimen straight from the reactions row
// set. Defensive: ignores any emoji outside the allow-list.
async function readReactionSummary(
  db: D1Database,
  specimenId: string
): Promise<{ reactions: Record<string, number>; total: number }> {
  const rows = await db
    .prepare("SELECT emoji, COUNT(*) AS c FROM reactions WHERE specimen_id = ? GROUP BY emoji")
    .bind(specimenId)
    .all<{ emoji: string; c: number }>();

  const reactions: Record<string, number> = {};
  let total = 0;
  for (const row of rows.results ?? []) {
    const count = Number(row.c ?? 0);
    if (isReactionEmoji(row.emoji) && count > 0) {
      reactions[row.emoji] = count;
      total += count;
    }
  }
  return { reactions, total };
}

// Every emoji a given user currently has on a specimen. Used for SSR initial
// state on the detail page so the user's own chips render pressed on first paint.
export async function getUserReactions(
  db: D1Database,
  specimenId: string,
  userId: string
): Promise<string[]> {
  const rows = await db
    .prepare("SELECT emoji FROM reactions WHERE specimen_id = ? AND user_id = ?")
    .bind(specimenId, userId)
    .all<{ emoji: string }>();
  return (rows.results ?? []).map((row) => row.emoji).filter(isReactionEmoji);
}

// Append a moderation report for a specimen. Reason is validated against
// REPORT_REASONS by the caller; status defaults to 'open' for the triage queue.
// Returns the new report id, the stored reason, and its status.
export async function createReport(
  db: D1Database,
  specimenId: string,
  userId: string,
  reason: ReportReason,
  details: string | null = null
): Promise<ReportResult> {
  const id = crypto.randomUUID();
  const status = "open";

  await db
    .prepare(
      `INSERT INTO reports (id, specimen_id, reporter_id, reason, details, status)
       VALUES (?, ?, ?, ?, ?, ?)`
    )
    .bind(id, specimenId, userId, reason, details, status)
    .run();

  return { id, reason, status };
}

// Resolve a specimen's primary-key id from its public slug. Returns null when no
// public specimen matches. Shared by the react/report routes (both address the
// specimen by slug but write rows keyed on the id).
export async function getSpecimenIdBySlug(
  db: D1Database,
  slug: string
): Promise<string | null> {
  const row = await db
    .prepare("SELECT id FROM specimens WHERE slug = ? AND visibility = 'public' LIMIT 1")
    .bind(slug)
    .first<{ id: string }>();
  return row?.id ?? null;
}
