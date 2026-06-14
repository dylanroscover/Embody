// Engagement domain: likes (toggle) and reports (create) for specimens.
//
// The `likes` and `reports` tables already exist (migrations/0001_init.sql).
// `likes` is a (user_id, specimen_id) composite-PK row set; the per-specimen
// total is denormalized onto `specimens.likes_count`. Toggling and the counter
// update are run as a single D1 batch so the row set and the denormalized count
// never drift. `reports` is an append-only moderation queue keyed by reporter.
//
// Request/response types live HERE (not packages/contracts/api.ts -- that file
// is owned by another agent). Keep these shapes self-contained.

// --- Public request/response types ----------------------------------------

// POST /api/specimens/:slug/like response. `liked` is the user's resulting
// state after the toggle; `likes_count` is the specimen's denormalized total.
export interface LikeToggleResult {
  liked: boolean;
  likes_count: number;
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

// Toggle a user's like on a specimen and keep the denormalized counter in step.
// If the (user, specimen) row exists it is removed and the count decremented;
// otherwise it is inserted and the count incremented. Both statements run in one
// D1 batch (atomic) so the row set and `specimens.likes_count` stay consistent.
// Returns the user's resulting like state and the specimen's new total.
export async function toggleLike(
  db: D1Database,
  specimenId: string,
  userId: string
): Promise<LikeToggleResult> {
  const existing = await db
    .prepare("SELECT 1 AS one FROM likes WHERE user_id = ? AND specimen_id = ? LIMIT 1")
    .bind(userId, specimenId)
    .first<{ one: number }>();

  const liked = !existing;

  if (liked) {
    await db.batch([
      db
        .prepare(
          "INSERT OR IGNORE INTO likes (user_id, specimen_id) VALUES (?, ?)"
        )
        .bind(userId, specimenId),
      // Recompute from the source-of-truth row set rather than blind +/- 1, so a
      // duplicate/no-op INSERT OR IGNORE can never desync the denormalized count.
      db
        .prepare(
          `UPDATE specimens
           SET likes_count = (SELECT COUNT(*) FROM likes WHERE specimen_id = ?)
           WHERE id = ?`
        )
        .bind(specimenId, specimenId)
    ]);
  } else {
    await db.batch([
      db
        .prepare("DELETE FROM likes WHERE user_id = ? AND specimen_id = ?")
        .bind(userId, specimenId),
      db
        .prepare(
          `UPDATE specimens
           SET likes_count = (SELECT COUNT(*) FROM likes WHERE specimen_id = ?)
           WHERE id = ?`
        )
        .bind(specimenId, specimenId)
    ]);
  }

  const row = await db
    .prepare("SELECT likes_count FROM specimens WHERE id = ? LIMIT 1")
    .bind(specimenId)
    .first<{ likes_count: number }>();

  return {
    liked,
    likes_count: Number(row?.likes_count ?? 0)
  };
}

// Whether a given user currently likes a specimen. Used for SSR initial state on
// the detail page so the button renders in the correct state on first paint.
export async function hasLiked(
  db: D1Database,
  specimenId: string,
  userId: string
): Promise<boolean> {
  const row = await db
    .prepare("SELECT 1 AS one FROM likes WHERE user_id = ? AND specimen_id = ? LIMIT 1")
    .bind(userId, specimenId)
    .first<{ one: number }>();
  return Boolean(row);
}

// Append a moderation report for a specimen. Reason is validated against
// REPORT_REASONS by the caller; status defaults to 'open' for the triage queue.
// Returns the new report id, the stored reason, and its status.
export async function createReport(
  db: D1Database,
  specimenId: string,
  userId: string,
  reason: ReportReason
): Promise<ReportResult> {
  const id = crypto.randomUUID();
  const status = "open";

  await db
    .prepare(
      `INSERT INTO reports (id, specimen_id, reporter_id, reason, status)
       VALUES (?, ?, ?, ?, ?)`
    )
    .bind(id, specimenId, userId, reason, status)
    .run();

  return { id, reason, status };
}

// Resolve a specimen's primary-key id from its public slug. Returns null when no
// public specimen matches. Shared by the like/report routes (both address the
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
