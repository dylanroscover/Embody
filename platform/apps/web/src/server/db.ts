import {
  emptyCapabilityCounts,
  MAX_CATEGORIES,
  type CapabilityJson,
  type Level,
  type ListResponse,
  type SearchResponse,
  type SpecimenDetail,
  type SpecimenSummary,
  type Tier,
  type Visibility
} from "@embody/contracts";
import type { RequestUser } from "./auth";
import { fixtureSummaries } from "../lib/specimenFallback";
import { parseReactions } from "../lib/reactions";

export type SpecimenSort = "recent" | "updated" | "popular" | "views" | "name";

// Collection-page sort vocabulary (newest | copied | az). Maps onto the
// keyset-paginated query in listSpecimens. Kept distinct from the legacy
// SpecimenSort union so existing callers (and their query string) are unchanged.
export type CollectionSort = "newest" | "copied" | "liked" | "az";

export type TrustLevel = "anon" | "verified" | "curator" | "admin";

export interface UserProfile {
  handle: string;
  /** Optional free-form display name; null falls back to "@handle" in the UI. */
  display_name: string | null;
  avatar_url: string | null;
  bio: string | null;
  trust_level: TrustLevel;
  created_at: string;
}

export interface ListSpecimensOptions {
  sort?: SpecimenSort;
  tag?: string;
  author?: string;
  page?: number;
  pageSize?: number;
}

export interface CollectionListOptions {
  /** Full-text query (FTS via specimens_fts). Empty/undefined = no text filter. */
  q?: string;
  category?: string;
  level?: string;
  requires?: string;
  /** Author handle facet. Empty/undefined = no author filter. */
  author?: string;
  sort?: CollectionSort;
  /** Opaque keyset cursor from a previous page's nextCursor. */
  cursor?: string;
  pageSize?: number;
}

// Decoded keyset cursor. `k` is the primary sort key value at the boundary row
// (created_at string, copies_count number, or title string); `slug` is the
// stable tiebreaker. `sort` guards against a cursor minted under a different
// sort being replayed.
interface CollectionCursor {
  sort: CollectionSort;
  k: string | number;
  slug: string;
}

export interface InsertSpecimenInput {
  user: RequestUser;
  title: string;
  description: string;
  tags: string[];
  license: string;
  /** Submit-form metadata; whitelist-validated upstream in the API route. */
  level?: Level;
  /** Known category facet; empty falls back to the first tag (legacy behavior). */
  category?: string;
  /** Full category set (1..MAX_CATEGORIES). First entry is the primary; falls
   *  back to `category` when absent. Whitelist-validated upstream in the route. */
  categories?: string[];
  /** Hardware/capability requirements (multi); empty list = stock TouchDesigner. */
  requires?: string[];
  /** 'public' or 'private'; anything else (or absent) defaults to 'private'
   *  (the author's draft -- they publish to 'public' to add it to the Collection). */
  visibility?: Visibility;
  tdnR2Key: string;
  tdnSha256: string;
  sizeBytes: number;
  scan: CapabilityJson;
  thumbnailKey?: string;
  /** R2 key for the cover video (videos/{sha256}). Omitted = image-only cover. */
  videoKey?: string;
  parsedTdn?: unknown;
}

export interface CurrentTdnBlob {
  key: string;
  capability: CapabilityJson;
}

interface SpecimenSummaryRow {
  slug: string;
  title: string;
  category: string;
  /** char(31)-joined full category set from specimen_categories (may be null). */
  categories_concat: string | null;
  level: string;
  description: string;
  requires: string;
  op_count: number;
  thumbnail_key: string | null;
  video_key: string | null;
  author_handle: string;
  author_avatar_url: string | null;
  tier: string;
  visibility: string;
  likes_count: number;
  reactions_summary: string | null;
  views_count: number;
  copies_count: number;
}

interface SpecimenDetailRow extends SpecimenSummaryRow {
  capability_json: string | null;
  current_version: number | null;
  created_at: string;
  updated_at: string;
}

// Collection rows carry created_at too (the keyset key for the "newest" sort).
interface CollectionRow extends SpecimenSummaryRow {
  created_at: string;
}

const DEFAULT_PAGE_SIZE = 24;
const MAX_PAGE_SIZE = 100;
const SUMMARY_COLUMNS = [
  "s.slug",
  "s.title",
  "s.category",
  "(SELECT GROUP_CONCAT(sc.category, char(31)) FROM specimen_categories sc WHERE sc.specimen_id = s.id) AS categories_concat",
  "s.level",
  "s.description",
  "s.requires",
  "s.op_count",
  "s.thumbnail_key",
  "s.video_key",
  "u.handle AS author_handle",
  "u.avatar_url AS author_avatar_url",
  "s.tier",
  "s.visibility",
  "s.likes_count",
  "s.reactions_summary",
  "s.views_count",
  "s.copies_count"
].join(", ");

// Collection query needs created_at on top of the summary columns (keyset key
// for the "newest" sort).
const COLLECTION_COLUMNS = `${SUMMARY_COLUMNS}, s.created_at`;

const SORT_SQL: Record<SpecimenSort, string> = {
  recent: "s.created_at DESC",
  updated: "s.updated_at DESC",
  popular: "s.likes_count DESC",
  views: "s.views_count DESC",
  name: "s.title COLLATE NOCASE ASC"
};

// Per collection sort: the indexed ORDER BY (with s.slug as the stable
// tiebreaker), the keyset comparison operator, the SQL expression that produces
// the cursor key, and how the key column reads off a row. Keyset pagination
// means each page is a single indexed range scan -- cost is O(pageSize), not
// O(offset), so it holds at thousands of rows.
interface CollectionSortPlan {
  // ORDER BY clause (primary key + slug tiebreaker), shared by count-free page reads.
  orderBy: string;
  // Comparison that selects rows strictly AFTER the cursor row, in sort order.
  // ? is the primary key value, then s.slug, then the key value again.
  keyset: string;
  // Reads the primary sort key value off a fetched collection row.
  keyOf: (row: CollectionRow) => string | number;
}

const COLLECTION_SORT_PLAN: Record<CollectionSort, CollectionSortPlan> = {
  // Newest first: created_at DESC, slug ASC. After a boundary row we want rows
  // with an older created_at, or the same created_at and a later slug.
  newest: {
    orderBy: "s.created_at DESC, s.slug ASC",
    keyset: "(s.created_at < ? OR (s.created_at = ? AND s.slug > ?))",
    keyOf: (row) => row.created_at
  },
  // Most copied first: copies_count DESC, slug ASC.
  copied: {
    orderBy: "s.copies_count DESC, s.slug ASC",
    keyset: "(s.copies_count < ? OR (s.copies_count = ? AND s.slug > ?))",
    keyOf: (row) => Number(row.copies_count ?? 0)
  },
  // Most liked first: likes_count (denormalized total reactions) DESC, slug ASC.
  liked: {
    orderBy: "s.likes_count DESC, s.slug ASC",
    keyset: "(s.likes_count < ? OR (s.likes_count = ? AND s.slug > ?))",
    keyOf: (row) => Number(row.likes_count ?? 0)
  },
  // A-Z: title (case-insensitive) ASC, slug ASC.
  az: {
    orderBy: "s.title COLLATE NOCASE ASC, s.slug ASC",
    keyset:
      "(s.title COLLATE NOCASE > ? OR (s.title COLLATE NOCASE = ? AND s.slug > ?))",
    keyOf: (row) => row.title
  }
};

export async function listSpecimens(
  db: D1Database,
  options: ListSpecimensOptions = {}
): Promise<ListResponse> {
  const page = normalizePage(options.page);
  const pageSize = normalizePageSize(options.pageSize);
  const offset = (page - 1) * pageSize;
  const sort = options.sort ?? "recent";
  const query = specimenListQuery(options);

  const countRow = await db
    .prepare(
      `SELECT COUNT(DISTINCT s.id) AS count
       FROM specimens s
       JOIN users_profile u ON u.id = s.author_id
       ${query.joins}
       WHERE ${query.where.join(" AND ")}`
    )
    .bind(...query.params)
    .first<{ count: number }>();

  const rows = await db
    .prepare(
      `SELECT ${SUMMARY_COLUMNS}
       FROM specimens s
       JOIN users_profile u ON u.id = s.author_id
       ${query.joins}
       WHERE ${query.where.join(" AND ")}
       ORDER BY ${SORT_SQL[sort]}, s.slug ASC
       LIMIT ? OFFSET ?`
    )
    .bind(...query.params, pageSize, offset)
    .all<SpecimenSummaryRow>();

  return {
    specimens: (rows.results ?? []).map(rowToSummary),
    count: Number(countRow?.count ?? 0),
    page,
    pageSize
  };
}

// Server-side list/filter/sort/paginate for the public collection page and its
// /api/specimens GET. Search is FTS (specimens_fts), facets are indexed equality
// filters, and pagination is KEYSET (cursor) so each page is a single bounded
// range scan -- O(pageSize), holding at thousands of rows. Returns the same
// ListResponse shape plus an opaque nextCursor (null on the last page).
export async function listSpecimensForCollection(
  db: D1Database,
  options: CollectionListOptions = {}
): Promise<ListResponse> {
  const pageSize = normalizePageSize(options.pageSize);
  const sort = normalizeCollectionSort(options.sort ?? "az");
  const plan = COLLECTION_SORT_PLAN[sort];

  // Public + non-banned author. A banned account's specimens drop out of every
  // listing here (reversible -- unban restores them, no per-row state).
  const where = ["s.visibility = 'public'", "u.banned = 0"];
  const joins: string[] = [];
  const filterParams: (string | number)[] = [];

  // FTS keyword filter: join the rowid mirror and MATCH. bm25 ordering is NOT
  // used here -- the page order is the chosen sort -- but MATCH still restricts
  // the candidate set cheaply via the FTS index.
  const matchQuery = options.q ? toFtsQuery(options.q) : "";
  if (matchQuery) {
    // FTS5 MATCH must reference the virtual table by name (no alias). Join on
    // rowid the same way searchSpecimensFts does; the MATCH predicate restricts
    // the candidate set via the FTS index before the facet/keyset filters run.
    joins.push("JOIN specimens_fts ON specimens_fts.rowid = s.rowid");
    where.push("specimens_fts MATCH ?");
    filterParams.push(matchQuery);
  }

  // Category is multi now: match specimens that have the facet among ANY of
  // their categories (the join table holds the full set, primary included).
  const category = (options.category ?? "").trim();
  if (category) {
    where.push(
      "EXISTS (SELECT 1 FROM specimen_categories sc WHERE sc.specimen_id = s.id AND sc.category = ?)"
    );
    filterParams.push(category);
  }

  const level = (options.level ?? "").trim();
  if (level) {
    where.push("s.level = ?");
    filterParams.push(level);
  }

  // requires is a JSON array; filter to specimens whose list CONTAINS the value.
  const requires = (options.requires ?? "").trim();
  if (requires) {
    // json_each throws on a malformed `requires` (a legacy scalar like 'none'),
    // which would crash the whole listing -- guard with json_valid so one bad
    // row degrades to "no requirements" instead of taking down the query.
    where.push(
      "EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(s.requires) THEN s.requires ELSE '[]' END) WHERE value = ?)"
    );
    filterParams.push(requires);
  }

  // Author facet: filter to one author by handle. The users_profile join (alias
  // u) is already present for the author_handle select, so this reuses it.
  const author = (options.author ?? "").trim();
  if (author) {
    where.push("u.handle = ?");
    filterParams.push(author);
  }

  const joinSql = joins.join("\n");
  const whereSql = where.join(" AND ");

  // Total matching the active filter set (server total for the count label).
  const countRow = await db
    .prepare(
      `SELECT COUNT(*) AS count
       FROM specimens s
       JOIN users_profile u ON u.id = s.author_id
       ${joinSql}
       WHERE ${whereSql}`
    )
    .bind(...filterParams)
    .first<{ count: number }>();

  // Keyset: when a cursor is supplied (and it matches the active sort), add the
  // "strictly after the boundary row" predicate so the scan resumes in place.
  const cursor = decodeCollectionCursor(options.cursor, sort);
  const pageWhere = [...where];
  const pageParams = [...filterParams];
  if (cursor) {
    pageWhere.push(plan.keyset);
    pageParams.push(cursor.k, cursor.k, cursor.slug);
  }

  // Over-fetch one row to detect whether a further page exists, without a
  // second query.
  const rows = await db
    .prepare(
      `SELECT ${COLLECTION_COLUMNS}
       FROM specimens s
       JOIN users_profile u ON u.id = s.author_id
       ${joinSql}
       WHERE ${pageWhere.join(" AND ")}
       ORDER BY ${plan.orderBy}
       LIMIT ?`
    )
    .bind(...pageParams, pageSize + 1)
    .all<CollectionRow>();

  const fetched = rows.results ?? [];
  const hasMore = fetched.length > pageSize;
  const pageRows = hasMore ? fetched.slice(0, pageSize) : fetched;
  const lastRow = pageRows[pageRows.length - 1];

  const nextCursor =
    hasMore && lastRow
      ? encodeCollectionCursor({ sort, k: plan.keyOf(lastRow), slug: lastRow.slug })
      : null;

  return {
    specimens: pageRows.map(rowToSummary),
    count: Number(countRow?.count ?? 0),
    page: 1,
    pageSize,
    nextCursor
  };
}

// Distinct facet values for the collection filter dropdowns. A handful of cheap
// SELECT DISTINCT reads over indexed columns -- run once at SSR to populate the
// category / requires <select>s with every value that exists, not just those on
// the first page (so the facets stay correct at thousands of rows).
export interface CollectionFacets {
  categories: string[];
  requires: string[];
  authors: string[];
}

export async function getCollectionFacets(db: D1Database): Promise<CollectionFacets> {
  const categories = await db
    .prepare(
      `SELECT DISTINCT sc.category AS value
       FROM specimen_categories sc
       JOIN specimens s ON s.id = sc.specimen_id
       WHERE s.visibility = 'public' AND sc.category <> ''
         AND s.author_id NOT IN (SELECT id FROM users_profile WHERE banned = 1)
       ORDER BY sc.category COLLATE NOCASE ASC`
    )
    .all<{ value: string }>();

  // requires is a JSON array per row; unnest with json_each to get the distinct
  // set of individual requirements present across all public specimens.
  const requires = await db
    .prepare(
      `SELECT DISTINCT je.value AS value
       FROM specimens s, json_each(CASE WHEN json_valid(s.requires) THEN s.requires ELSE '[]' END) je
       WHERE s.visibility = 'public' AND je.value <> ''
         AND s.author_id NOT IN (SELECT id FROM users_profile WHERE banned = 1)
       ORDER BY je.value COLLATE NOCASE ASC`
    )
    .all<{ value: string }>();

  // Authors who have at least one public specimen, by handle. Joins the profile
  // table (same alias/condition the listing uses) so the dropdown only ever
  // offers handles that actually filter to something.
  const authors = await db
    .prepare(
      `SELECT DISTINCT u.handle AS value
       FROM specimens s
       JOIN users_profile u ON u.id = s.author_id
       WHERE s.visibility = 'public' AND u.banned = 0 AND u.handle <> ''
       ORDER BY u.handle COLLATE NOCASE ASC`
    )
    .all<{ value: string }>();

  return {
    categories: (categories.results ?? []).map((row) => row.value),
    requires: (requires.results ?? []).map((row) => row.value),
    authors: (authors.results ?? []).map((row) => row.value)
  };
}

export async function getSpecimenBySlug(
  db: D1Database,
  slug: string,
  // When set to the signed-in user's id, that user can also load their OWN
  // non-public (private/unlisted) specimens -- so an author can view a draft to
  // verify it before publishing. Anonymous/other viewers still only see public.
  viewerId?: string | null
): Promise<(SpecimenDetail & { author_display_name: string | null }) | null> {
  // Tags are folded into the detail row via a correlated GROUP_CONCAT subquery
  // (delimited by char(31), the ASCII unit separator, so a comma inside a tag
  // name can't corrupt the split), collapsing the prior 2-query N+1 into one
  // round trip. The inner ORDER BY in the derived table feeds GROUP_CONCAT its
  // rows in case-insensitive name order, matching the old separate query.
  const row = await db
    .prepare(
      `SELECT ${SUMMARY_COLUMNS},
              u.display_name AS author_display_name,
              s.capability_json,
              s.license,
              v.version_num AS current_version,
              s.created_at,
              s.updated_at,
              (SELECT GROUP_CONCAT(t.name, char(31))
                 FROM (
                   SELECT t.name
                   FROM tags t
                   JOIN specimen_tags st ON st.tag_id = t.id
                   WHERE st.specimen_id = s.id
                   ORDER BY t.name COLLATE NOCASE ASC
                 ) AS t
              ) AS tag_names
       FROM specimens s
       JOIN users_profile u ON u.id = s.author_id
       LEFT JOIN specimen_versions v ON v.id = s.current_version_id
       WHERE s.slug = ? AND u.banned = 0 AND (s.visibility = 'public' OR s.author_id = ?)
       LIMIT 1`
    )
    .bind(slug, viewerId ?? "")
    .first<
      SpecimenDetailRow & {
        tag_names: string | null;
        author_display_name: string | null;
        license: string;
      }
    >();

  if (!row) return null;

  // GROUP_CONCAT joined the tag names on char(31) (the ASCII unit separator, a
  // byte that cannot appear in a tag name); split on the same. Built from
  // fromCharCode so the source file stays printable ASCII, no raw control byte.
  const TAG_SEP = String.fromCharCode(31);
  const tags = row.tag_names
    ? row.tag_names.split(TAG_SEP).filter(Boolean)
    : [];

  return {
    ...rowToSummary(row),
    author_display_name: row.author_display_name,
    capability: parseCapability(row.capability_json),
    current_version: Number(row.current_version ?? 1),
    tags,
    license: row.license || "CC-BY-4.0",
    created_at: row.created_at,
    updated_at: row.updated_at
  };
}

export async function getUserByHandle(
  db: D1Database,
  handle: string
): Promise<UserProfile | null> {
  const trimmed = handle.trim();
  if (!trimmed) return null;

  try {
    const row = await db
      .prepare(
        `SELECT handle, display_name, avatar_url, bio, trust_level, created_at
         FROM users_profile
         WHERE handle = ?
         LIMIT 1`
      )
      .bind(trimmed)
      .first<{
        handle: string;
        display_name: string | null;
        avatar_url: string | null;
        bio: string | null;
        trust_level: string;
        created_at: string;
      }>();

    if (row) {
      return {
        handle: row.handle,
        display_name: row.display_name,
        avatar_url: row.avatar_url,
        bio: row.bio,
        trust_level: normalizeTrustLevel(row.trust_level),
        created_at: row.created_at
      };
    }
  } catch {
    // D1 unavailable in local dev; fall through to the fixture profile.
  }

  return fallbackUserProfile(trimmed);
}

// Set (or clear with null) a user's effective avatar_url. Used by the avatar
// upload/remove routes; clearing reverts every avatar site to the letter chip.
export async function setProfileAvatarUrl(
  db: D1Database,
  userId: string,
  avatarUrl: string | null
): Promise<void> {
  await db
    .prepare("UPDATE users_profile SET avatar_url = ? WHERE id = ?")
    .bind(avatarUrl, userId)
    .run();
}

export async function listSpecimensByAuthor(
  db: D1Database,
  handle: string,
  // When set to the signed-in user's id, the author's OWN non-public specimens
  // (private drafts / unlisted) are included too -- so a user sees their drafts
  // on their own profile (badged), while visitors see only the public set.
  viewerId?: string | null
): Promise<SpecimenSummary[]> {
  const trimmed = handle.trim();
  if (!trimmed) return [];

  try {
    const rows = await db
      .prepare(
        `SELECT ${SUMMARY_COLUMNS}
         FROM specimens s
         JOIN users_profile u ON u.id = s.author_id
         WHERE u.handle = ? AND u.banned = 0 AND (s.visibility = 'public' OR s.author_id = ?)
         ORDER BY s.created_at DESC, s.slug ASC`
      )
      .bind(trimmed, viewerId ?? "")
      .all<SpecimenSummaryRow>();

    const mapped = (rows.results ?? []).map(rowToSummary);
    if (mapped.length > 0) return mapped;
  } catch {
    // D1 unavailable in local dev; fall through to the fixture set.
  }

  return fallbackSpecimensByAuthor(trimmed);
}

// Fallback profile derived from the fixture set, so /u/<handle> works in dev
// (empty/unreachable D1) exactly like the collection page does. Only the
// curator handle that backs the fixtures resolves; any other handle is null.
function fallbackUserProfile(handle: string): UserProfile | null {
  if (!fallbackSpecimensByAuthor(handle).length) return null;

  return {
    handle,
    display_name: null,
    avatar_url: null,
    bio: "First-party Embody specimen author. Curating the transparent TDN Collection.",
    trust_level: "curator",
    created_at: "2026-01-01T00:00:00Z"
  };
}

// Handles reserved for routes / system use -- never assignable to a user.
const RESERVED_HANDLES = new Set([
  "admin", "administrator", "api", "app", "about", "account", "settings", "auth",
  "collection", "contribute", "manifesto", "signin", "signup", "register", "login",
  "logout", "u", "c", "embody", "envoy", "tdn", "support", "help", "terms",
  "privacy", "static", "assets", "public", "new", "edit", "me", "profile",
  "dashboard", "null", "undefined", "anon", "anonymous", "system", "root", "home",
  "404", "favicon", "robots", "sitemap"
]);

// Validate a user-chosen handle: 3-30 chars, lowercase a-z0-9 with single hyphens
// (no leading / trailing / double hyphen), not reserved. Returns the normalized
// (trimmed + lowercased) handle.
export function validateHandle(
  raw: string
): { ok: true; handle: string } | { ok: false; detail: string } {
  const handle = raw.trim().toLowerCase();
  if (handle.length < 3 || handle.length > 30) {
    return { ok: false, detail: "Handle must be 3-30 characters." };
  }
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(handle)) {
    return {
      ok: false,
      detail: "Handle may use lowercase letters, numbers, and single hyphens (no leading, trailing, or double hyphens)."
    };
  }
  if (RESERVED_HANDLES.has(handle)) {
    return { ok: false, detail: "That handle is reserved." };
  }
  return { ok: true, handle };
}

// Owner edit of identity: the unique URL handle + the optional display name.
// Validates + enforces handle uniqueness (excluding self). Returns the saved
// handle, or a reason it was rejected.
//
// NOTE: the specimens_fts mirror denormalizes author_handle, so after a handle
// change a FREE-TEXT search for the OLD handle can still match until that specimen
// is next re-synced (re-edited). The "by user" FILTER joins live users_profile, so
// it reflects the new handle immediately -- the only staleness is search text.
export async function updateUserProfile(
  db: D1Database,
  input: { userId: string; handle: string; displayName: string | null }
): Promise<{ ok: true; handle: string } | { ok: false; detail: string }> {
  const v = validateHandle(input.handle);
  if (!v.ok) return v;

  const taken = await db
    .prepare("SELECT 1 AS hit FROM users_profile WHERE handle = ? AND id != ? LIMIT 1")
    .bind(v.handle, input.userId)
    .first<{ hit: number }>();
  if (taken) return { ok: false, detail: "That handle is already taken." };

  const display = (input.displayName ?? "").trim().slice(0, 60) || null;
  await db
    .prepare("UPDATE users_profile SET handle = ?, display_name = ? WHERE id = ?")
    .bind(v.handle, display, input.userId)
    .run();

  return { ok: true, handle: v.handle };
}

function fallbackSpecimensByAuthor(handle: string): SpecimenSummary[] {
  return fixtureSummaries.filter((summary) => summary.author_handle === handle);
}

export async function getCurrentTdnBlobForSlug(
  db: D1Database,
  slug: string,
  // When set to the signed-in user's id, that user can also load their OWN
  // non-public draft's network (preview / edit prefill / edit diff). Left unset
  // by the public /tdn + /copy endpoints, which must only ever serve public TDN.
  viewerId?: string | null
): Promise<CurrentTdnBlob | null> {
  const row = await db
    .prepare(
      `SELECT v.tdn_r2_key AS key,
              s.capability_json
       FROM specimens s
       JOIN specimen_versions v ON v.id = s.current_version_id
       JOIN users_profile u ON u.id = s.author_id
       WHERE s.slug = ? AND u.banned = 0 AND (s.visibility = 'public' OR s.author_id = ?)
       LIMIT 1`
    )
    .bind(slug, viewerId ?? "")
    .first<{ key: string | null; capability_json: string | null }>();

  if (!row?.key) return null;

  return {
    key: row.key,
    capability: parseCapability(row.capability_json)
  };
}

export async function insertSpecimenWithVersion(
  db: D1Database,
  input: InsertSpecimenInput
): Promise<{ slug: string }> {
  const tags = normalizeTags(input.tags);
  const specimenId = crypto.randomUUID();
  const versionId = crypto.randomUUID();
  const scanId = crypto.randomUUID();
  const slug = await uniqueSlug(db, input.title);
  const title = input.title.trim();
  const description = input.description.trim();
  const capabilityJson = JSON.stringify(input.scan);
  const opCount = countTdnOperators(input.parsedTdn);
  // Categories (multi). Prefer the submit-form list; fall back to the legacy
  // single category, then the first tag's slug, then "community". The first
  // entry is the PRIMARY, stored in specimens.category (single-slot display +
  // back-compat); the full set goes to the specimen_categories join table.
  const primaryFallback = (input.category ?? "").trim() || tags[0]?.slug || "community";
  const categories = normalizeCategories(input.categories, primaryFallback);
  const category = categories[0] ?? primaryFallback;
  const level = normalizeLevel(input.level ?? "intermediate");
  const requires = serializeRequires(input.requires);
  const scanStatus = input.scan.verdict;
  const license = input.license.trim() || "CC-BY-4.0";
  // Only the binary public/private states are settable on submit; anything else
  // (or absent) defaults to 'private' so a new upload is a draft until published.
  const visibility = input.visibility === "public" ? "public" : "private";

  const statements: D1PreparedStatement[] = [
    db
      .prepare(
        `INSERT OR IGNORE INTO users_profile (id, handle, trust_level)
         VALUES (?, ?, 'verified')`
      )
      .bind(input.user.id, input.user.handle),
    db
      .prepare(
        `INSERT INTO specimens (
           id, slug, author_id, title, description, category, level, requires,
           op_count, family_summary, current_version_id, thumbnail_key, video_key,
           license, visibility, tier, scan_status, capability_json
         )
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?,
                 'community', ?, ?)`
      )
      .bind(
        specimenId,
        slug,
        input.user.id,
        title,
        description,
        category,
        level,
        requires,
        opCount,
        versionId,
        input.thumbnailKey ?? "",
        input.videoKey ?? null,
        license,
        visibility,
        scanStatus,
        capabilityJson
      ),
    db
      .prepare(
        `INSERT INTO specimen_versions (
           id, specimen_id, version_num, tdn_r2_key, tdn_sha256, size_bytes,
           op_count, scan_id, signature_ref, changelog
         )
         VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL, NULL)`
      )
      .bind(
        versionId,
        specimenId,
        input.tdnR2Key,
        input.tdnSha256,
        input.sizeBytes,
        opCount,
        scanId
      ),
    db
      .prepare(
        `INSERT INTO scans (
           id, version_id, scanner_version, verdict, capability_json, findings_json
         )
         VALUES (?, ?, ?, ?, ?, ?)`
      )
      .bind(
        scanId,
        versionId,
        input.scan.scanner_version,
        input.scan.verdict,
        capabilityJson,
        JSON.stringify(input.scan.findings)
      )
  ];

  for (const tag of tags) {
    statements.push(
      db
        .prepare("INSERT OR IGNORE INTO tags (id, name, slug) VALUES (?, ?, ?)")
        .bind(`tag-${tag.slug}`, tag.name, tag.slug),
      db
        .prepare("INSERT OR IGNORE INTO specimen_tags (specimen_id, tag_id) VALUES (?, ?)")
        .bind(specimenId, `tag-${tag.slug}`)
    );
  }

  for (const cat of categories) {
    statements.push(
      db
        .prepare("INSERT OR IGNORE INTO specimen_categories (specimen_id, category) VALUES (?, ?)")
        .bind(specimenId, cat)
    );
  }

  await db.batch(statements);
  await syncSpecimensFts(db, {
    specimenId,
    slug,
    title,
    description,
    tags: tags.map((tag) => tag.name),
    authorHandle: input.user.handle,
    datText: extractDatText(input.parsedTdn)
  });

  // TODO: Queue follow-up jobs for generated thumbnails and Sigstore signing.
  return { slug };
}

// Editable fields + ownership keys for a specimen, by slug. Backs BOTH the
// owner-only edit form (prefill) and the edit/delete API ownership check
// (authorId vs the signed-in user's id). license is included here because the
// shared SUMMARY_COLUMNS set omits it.
export interface SpecimenEditData {
  id: string;
  authorId: string;
  authorHandle: string;
  slug: string;
  title: string;
  description: string;
  tags: string[];
  license: string;
  level: string;
  category: string;
  categories: string[];
  requires: string[];
  visibility: Visibility;
}

// Load a specimen by slug for an owner action (edit prefill, publish toggle,
// delete). Intentionally OWNER-AGNOSTIC and visibility-agnostic -- it loads any
// specimen by slug so a private draft can be edited/published. Callers MUST gate
// on `authorId === user.id` (resolveOwner in the API; the edit page guard).
export async function getSpecimenForEdit(
  db: D1Database,
  slug: string
): Promise<SpecimenEditData | null> {
  const row = await db
    .prepare(
      `SELECT s.id, s.author_id, u.handle AS author_handle, s.slug, s.title,
              s.description, s.license, s.level, s.category, s.requires, s.visibility,
              (SELECT GROUP_CONCAT(t.name, char(31))
                 FROM specimen_tags st
                 JOIN tags t ON t.id = st.tag_id
                WHERE st.specimen_id = s.id) AS tag_names,
              (SELECT GROUP_CONCAT(sc.category, char(31))
                 FROM specimen_categories sc
                WHERE sc.specimen_id = s.id) AS categories_concat
         FROM specimens s
         JOIN users_profile u ON u.id = s.author_id
        WHERE s.slug = ?
        LIMIT 1`
    )
    .bind(slug)
    .first<{
      id: string;
      author_id: string;
      author_handle: string;
      slug: string;
      title: string;
      description: string | null;
      license: string;
      level: string;
      category: string;
      requires: string;
      visibility: string;
      tag_names: string | null;
      categories_concat: string | null;
    }>();

  if (!row) return null;

  const TAG_SEP = String.fromCharCode(31);
  return {
    id: row.id,
    authorId: row.author_id,
    authorHandle: row.author_handle,
    slug: row.slug,
    title: row.title,
    description: row.description ?? "",
    license: row.license,
    level: row.level,
    category: row.category,
    categories: buildCategories(row.category, row.categories_concat),
    requires: parseRequires(row.requires),
    tags: row.tag_names ? row.tag_names.split(TAG_SEP).filter(Boolean) : [],
    visibility: normalizeVisibility(row.visibility)
  };
}

// Owner edit of a specimen's METADATA (title/description/tags/license/
// level/category/requires). The TDN body is NOT touched here -- changing
// the network would require a re-scan + a new specimen_versions row, which is a
// separate "new version" path. parsedTdn (the unchanged current network) is
// passed only so the FTS mirror's dat_text is preserved on re-sync: syncSpecimensFts
// does INSERT OR REPLACE on the whole row, so omitting dat_text would wipe it.
// Set (or clear) ONLY a specimen's cover-video key, without touching any other
// metadata or the FTS mirror. Used by the video attach/replace/remove route -- a
// single-column UPDATE is both cheaper than a full updateSpecimenMetadata re-sync
// and safe: it cannot disturb tags, categories, or the FTS dat_text. Pass null to
// remove the cover video (clears video_key to NULL).
export async function setSpecimenVideoKey(
  db: D1Database,
  specimenId: string,
  videoKey: string | null
): Promise<void> {
  await db
    .prepare("UPDATE specimens SET video_key = ?, updated_at = datetime('now') WHERE id = ?")
    .bind(videoKey, specimenId)
    .run();
}

export async function updateSpecimenMetadata(
  db: D1Database,
  input: {
    specimenId: string;
    slug: string;
    authorHandle: string;
    title: string;
    description: string;
    tags: string[];
    license: string;
    level?: string;
    category?: string;
    categories?: string[];
    requires?: string[];
    /** New R2 thumbnail key. Omitted/undefined leaves the existing thumbnail untouched. */
    thumbnailKey?: string;
    /** New R2 cover-video key. Omitted/undefined leaves the existing video untouched. */
    videoKey?: string;
    /** Explicitly clear video_key to NULL (remove the cover video). Distinct from
     * an omitted videoKey, which leaves the existing video as-is. When true,
     * videoKey is ignored. */
    clearVideo?: boolean;
    parsedTdn?: Record<string, unknown> | null;
  }
): Promise<void> {
  const tags = normalizeTags(input.tags);
  const title = input.title.trim();
  const description = input.description.trim();
  const primaryFallback = (input.category ?? "").trim() || tags[0]?.slug || "community";
  const categories = normalizeCategories(input.categories, primaryFallback);
  const category = categories[0] ?? primaryFallback;
  const level = normalizeLevel(input.level ?? "intermediate");
  const requires = serializeRequires(input.requires);
  const license = input.license.trim() || "CC-BY-4.0";

  // Only overwrite thumbnail_key when a new key is supplied -- an edit that
  // doesn't change the image leaves the column as-is.
  const newThumb = typeof input.thumbnailKey === "string" && input.thumbnailKey ? input.thumbnailKey : null;
  // The cover video has THREE states: leave as-is (omitted/undefined), set to a
  // new key, or explicitly clear to NULL (clearVideo). clearVideo wins over any
  // supplied videoKey; a supplied key overwrites; omitting both leaves it as-is.
  const newVideo = typeof input.videoKey === "string" && input.videoKey ? input.videoKey : null;
  const touchVideo = input.clearVideo === true || newVideo !== null;

  // Build the optional trailing SET fragments (thumbnail then video) and their
  // bind values in lock-step so the placeholders and params stay aligned. When
  // clearing, video_key is set to NULL inline (no bind param).
  const setVideo = touchVideo ? (input.clearVideo === true ? ", video_key = NULL" : ", video_key = ?") : "";
  const setExtra = `${newThumb ? ", thumbnail_key = ?" : ""}${setVideo}`;
  const setExtraParams: (string | number)[] = [];
  if (newThumb) setExtraParams.push(newThumb);
  if (touchVideo && input.clearVideo !== true && newVideo) setExtraParams.push(newVideo);

  const statements: D1PreparedStatement[] = [
    db
      .prepare(
        `UPDATE specimens
            SET title = ?, description = ?, category = ?, level = ?,
                requires = ?, license = ?${setExtra}, updated_at = datetime('now')
          WHERE id = ?`
      )
      .bind(
        title,
        description,
        category,
        level,
        requires,
        license,
        ...setExtraParams,
        input.specimenId
      ),
    // Tags are a full replace: drop the existing links, re-add the new set.
    db.prepare("DELETE FROM specimen_tags WHERE specimen_id = ?").bind(input.specimenId),
    // Categories are a full replace too.
    db.prepare("DELETE FROM specimen_categories WHERE specimen_id = ?").bind(input.specimenId)
  ];

  for (const tag of tags) {
    statements.push(
      db
        .prepare("INSERT OR IGNORE INTO tags (id, name, slug) VALUES (?, ?, ?)")
        .bind(`tag-${tag.slug}`, tag.name, tag.slug),
      db
        .prepare("INSERT OR IGNORE INTO specimen_tags (specimen_id, tag_id) VALUES (?, ?)")
        .bind(input.specimenId, `tag-${tag.slug}`)
    );
  }

  for (const cat of categories) {
    statements.push(
      db
        .prepare("INSERT OR IGNORE INTO specimen_categories (specimen_id, category) VALUES (?, ?)")
        .bind(input.specimenId, cat)
    );
  }

  await db.batch(statements);

  // Re-sync the FTS mirror with the new metadata (see db.ts:692 TODO note).
  await syncSpecimensFts(db, {
    specimenId: input.specimenId,
    slug: input.slug,
    title,
    description,
    tags: tags.map((tag) => tag.name),
    authorHandle: input.authorHandle,
    datText: input.parsedTdn ? extractDatText(input.parsedTdn) : ""
  });
}

// Owner edit of a specimen's NETWORK (the TDN body). Appends a new
// specimen_versions row + its scan, repoints current_version_id, and refreshes
// the specimen's op_count / scan_status / capability_json + the FTS dat_text. The
// caller MUST have already parsed + scanned the new TDN (same gates as submit:
// blocked verdict / obvious-malware are rejected BEFORE this runs) and stored the
// blob. Metadata (title/tags/etc.) is updated separately via updateSpecimenMetadata.
export async function addSpecimenVersion(
  db: D1Database,
  input: {
    specimenId: string;
    slug: string;
    authorHandle: string;
    title: string;
    description: string;
    tags: string[];
    tdnR2Key: string;
    tdnSha256: string;
    sizeBytes: number;
    scan: CapabilityJson;
    parsedTdn: Record<string, unknown>;
  }
): Promise<{ versionNum: number }> {
  const versionId = crypto.randomUUID();
  const scanId = crypto.randomUUID();
  const capabilityJson = JSON.stringify(input.scan);
  const opCount = countTdnOperators(input.parsedTdn);
  const scanStatus = input.scan.verdict;

  // Next version number for this specimen (current max + 1).
  const maxRow = await db
    .prepare("SELECT MAX(version_num) AS n FROM specimen_versions WHERE specimen_id = ?")
    .bind(input.specimenId)
    .first<{ n: number | null }>();
  const versionNum = Number(maxRow?.n ?? 0) + 1;

  await db.batch([
    db
      .prepare(
        `INSERT INTO specimen_versions (
           id, specimen_id, version_num, tdn_r2_key, tdn_sha256, size_bytes,
           op_count, scan_id, signature_ref, changelog
         )
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)`
      )
      .bind(
        versionId,
        input.specimenId,
        versionNum,
        input.tdnR2Key,
        input.tdnSha256,
        input.sizeBytes,
        opCount,
        scanId
      ),
    db
      .prepare(
        `INSERT INTO scans (
           id, version_id, scanner_version, verdict, capability_json, findings_json
         )
         VALUES (?, ?, ?, ?, ?, ?)`
      )
      .bind(
        scanId,
        versionId,
        input.scan.scanner_version,
        input.scan.verdict,
        capabilityJson,
        JSON.stringify(input.scan.findings)
      ),
    db
      .prepare(
        `UPDATE specimens
            SET current_version_id = ?, op_count = ?, scan_status = ?,
                capability_json = ?, updated_at = datetime('now')
          WHERE id = ?`
      )
      .bind(versionId, opCount, scanStatus, capabilityJson, input.specimenId)
  ]);

  await syncSpecimensFts(db, {
    specimenId: input.specimenId,
    slug: input.slug,
    title: input.title.trim(),
    description: input.description.trim(),
    tags: normalizeTags(input.tags).map((tag) => tag.name),
    authorHandle: input.authorHandle,
    datText: extractDatText(input.parsedTdn)
  });

  return { versionNum };
}

// Set a specimen's visibility (the owner's publish/unpublish toggle, or an admin
// moderation change). Returns false when no specimen has that id. updated_at is
// bumped so the change surfaces in any "recently updated" view. The public
// listing/detail queries filter on visibility, so flipping to 'private' removes
// it from the Collection immediately and 'public' adds it back.
export async function setSpecimenVisibility(
  db: D1Database,
  specimenId: string,
  visibility: Visibility
): Promise<boolean> {
  const result = await db
    .prepare("UPDATE specimens SET visibility = ?, updated_at = datetime('now') WHERE id = ?")
    .bind(visibility, specimenId)
    .run();
  return (result.meta?.changes ?? 0) > 0;
}

// Hard-delete a specimen and every row that references it. FK order matters:
// scans -> specimen_versions -> (tags/likes/comments/reports/reactions) -> the
// specimen itself. The final delete fires the AFTER DELETE trigger from
// migration 0005, which removes the contentless-delete FTS mirror row by rowid.
// Runs as a single D1 batch (atomic transaction).
export async function deleteSpecimenById(db: D1Database, specimenId: string): Promise<void> {
  await db.batch([
    db
      .prepare(
        `DELETE FROM scans
          WHERE version_id IN (SELECT id FROM specimen_versions WHERE specimen_id = ?)`
      )
      .bind(specimenId),
    db.prepare("DELETE FROM specimen_versions WHERE specimen_id = ?").bind(specimenId),
    db.prepare("DELETE FROM specimen_tags WHERE specimen_id = ?").bind(specimenId),
    db.prepare("DELETE FROM specimen_categories WHERE specimen_id = ?").bind(specimenId),
    db.prepare("DELETE FROM likes WHERE specimen_id = ?").bind(specimenId),
    db.prepare("DELETE FROM comments WHERE specimen_id = ?").bind(specimenId),
    db.prepare("DELETE FROM reports WHERE specimen_id = ?").bind(specimenId),
    db.prepare("DELETE FROM reactions WHERE specimen_id = ?").bind(specimenId),
    db.prepare("DELETE FROM specimens WHERE id = ?").bind(specimenId)
  ]);
}

// Hard-delete an entire user account: every specimen they authored (and all of
// each specimen's dependents), all of their engagement on OTHER people's
// specimens (reactions/likes/comments/reports), their profile row, and their
// Better Auth identity (session/account/user). One atomic D1 batch. The bulk
// `DELETE FROM specimens WHERE author_id` fires the per-row FTS delete trigger
// (migration 0005), keeping the search mirror consistent. session/account also
// cascade off `user` (migration 0003), but they are deleted explicitly first so
// this holds regardless of D1's FK-enforcement state. Returns false if no such
// profile exists (nothing deleted).
export async function deleteUserAccount(db: D1Database, userId: string): Promise<boolean> {
  const exists = await db
    .prepare("SELECT 1 AS hit FROM users_profile WHERE id = ? LIMIT 1")
    .bind(userId)
    .first<{ hit: number }>();
  if (!exists) return false;

  const ownSpecimens = "(SELECT id FROM specimens WHERE author_id = ?)";
  await db.batch([
    // 1. Dependents of the user's OWN specimens.
    db
      .prepare(
        `DELETE FROM scans WHERE version_id IN
           (SELECT v.id FROM specimen_versions v
              JOIN specimens s ON s.id = v.specimen_id
             WHERE s.author_id = ?)`
      )
      .bind(userId),
    db.prepare(`DELETE FROM specimen_versions WHERE specimen_id IN ${ownSpecimens}`).bind(userId),
    db.prepare(`DELETE FROM specimen_tags WHERE specimen_id IN ${ownSpecimens}`).bind(userId),
    db.prepare(`DELETE FROM specimen_categories WHERE specimen_id IN ${ownSpecimens}`).bind(userId),
    db.prepare(`DELETE FROM likes WHERE specimen_id IN ${ownSpecimens}`).bind(userId),
    db.prepare(`DELETE FROM comments WHERE specimen_id IN ${ownSpecimens}`).bind(userId),
    db.prepare(`DELETE FROM reports WHERE specimen_id IN ${ownSpecimens}`).bind(userId),
    db.prepare(`DELETE FROM reactions WHERE specimen_id IN ${ownSpecimens}`).bind(userId),
    db.prepare("DELETE FROM specimens WHERE author_id = ?").bind(userId),
    // 2. The user's engagement on OTHER people's specimens.
    db.prepare("DELETE FROM reactions WHERE user_id = ?").bind(userId),
    db.prepare("DELETE FROM likes WHERE user_id = ?").bind(userId),
    db.prepare("DELETE FROM comments WHERE author_id = ?").bind(userId),
    db.prepare("DELETE FROM reports WHERE reporter_id = ?").bind(userId),
    // 3. Profile + Better Auth identity (session/account before user).
    db.prepare("DELETE FROM users_profile WHERE id = ?").bind(userId),
    db.prepare("DELETE FROM session WHERE userId = ?").bind(userId),
    db.prepare("DELETE FROM account WHERE userId = ?").bind(userId),
    db.prepare('DELETE FROM "user" WHERE id = ?').bind(userId)
  ]);
  return true;
}

// --- Ban / suspend ---------------------------------------------------------

// Ban or unban an account. A banned profile makes getSessionUser return null
// (so they cannot sign in or submit) and drops their specimens from every public
// query (the `u.banned = 0` filters above). Fully reversible. Returns false when
// no such profile exists.
export async function setUserBanned(
  db: D1Database,
  userId: string,
  banned: boolean,
  reason?: string | null
): Promise<boolean> {
  const stmt = banned
    ? db
        .prepare(
          "UPDATE users_profile SET banned = 1, banned_reason = ?, banned_at = datetime('now') WHERE id = ?"
        )
        .bind(reason ?? null, userId)
    : db
        .prepare(
          "UPDATE users_profile SET banned = 0, banned_reason = NULL, banned_at = NULL WHERE id = ?"
        )
        .bind(userId);
  const result = await stmt.run();
  return (result.meta?.changes ?? 0) > 0;
}

// Resolve a specimen's author id (used by the report auto-ban flow).
export async function getSpecimenAuthorId(
  db: D1Database,
  specimenId: string
): Promise<string | null> {
  const row = await db
    .prepare("SELECT author_id FROM specimens WHERE id = ? LIMIT 1")
    .bind(specimenId)
    .first<{ author_id: string }>();
  return row?.author_id ?? null;
}

// Distinct reporters across all still-open reports on ONE specimen (auto-hide
// signal). DISTINCT so a single person can't inflate the count by re-reporting.
export async function countDistinctReportersForSpecimen(
  db: D1Database,
  specimenId: string
): Promise<number> {
  const row = await db
    .prepare(
      `SELECT COUNT(DISTINCT reporter_id) AS n FROM reports
        WHERE specimen_id = ? AND status IN ('open', 'reviewing')`
    )
    .bind(specimenId)
    .first<{ n: number }>();
  return Number(row?.n ?? 0);
}

// Distinct reporters across all still-open reports on EVERY specimen an author
// owns (auto-ban signal). DISTINCT so one mass-reporter can't trigger a ban.
export async function countDistinctReportersForAuthor(
  db: D1Database,
  authorId: string
): Promise<number> {
  const row = await db
    .prepare(
      `SELECT COUNT(DISTINCT r.reporter_id) AS n
         FROM reports r
         JOIN specimens s ON s.id = r.specimen_id
        WHERE s.author_id = ? AND r.status IN ('open', 'reviewing')`
    )
    .bind(authorId)
    .first<{ n: number }>();
  return Number(row?.n ?? 0);
}

// --- Audit log -------------------------------------------------------------

export interface AuditEvent {
  actorId?: string | null;
  actorHandle?: string | null;
  action: string;
  targetType?: string | null;
  targetId?: string | null;
  metadata?: Record<string, unknown> | null;
  ip?: string | null;
}

export interface AuditLogRow {
  id: string;
  ts: string;
  actor_id: string | null;
  actor_handle: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  metadata: string | null;
  ip: string | null;
}

// Record a security / abuse / admin event: append to audit_log AND emit a
// structured console line (captured by Cloudflare Workers Logs for the hybrid
// telemetry layer). Best-effort -- a logging failure is swallowed so it never
// breaks the action it is recording.
export async function logEvent(db: D1Database, event: AuditEvent): Promise<void> {
  console.log(
    "AUDIT",
    JSON.stringify({
      action: event.action,
      actor: event.actorId ?? null,
      actorHandle: event.actorHandle ?? null,
      targetType: event.targetType ?? null,
      target: event.targetId ?? null,
      metadata: event.metadata ?? null,
      ip: event.ip ?? null
    })
  );
  try {
    await db
      .prepare(
        `INSERT INTO audit_log
           (id, actor_id, actor_handle, action, target_type, target_id, metadata, ip)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .bind(
        crypto.randomUUID(),
        event.actorId ?? null,
        event.actorHandle ?? null,
        event.action,
        event.targetType ?? null,
        event.targetId ?? null,
        event.metadata ? JSON.stringify(event.metadata) : null,
        event.ip ?? null
      )
      .run();
  } catch (error) {
    console.error("logEvent: audit insert failed", error);
  }
}

export async function listAuditLog(
  db: D1Database,
  opts: { action?: string; actorId?: string; targetId?: string; limit?: number } = {}
): Promise<AuditLogRow[]> {
  const limit = Math.min(Math.max(opts.limit ?? 100, 1), 200);
  const clauses: string[] = [];
  const vals: (string | number)[] = [];
  if (opts.action) {
    clauses.push("action = ?");
    vals.push(opts.action);
  }
  if (opts.actorId) {
    clauses.push("actor_id = ?");
    vals.push(opts.actorId);
  }
  if (opts.targetId) {
    clauses.push("target_id = ?");
    vals.push(opts.targetId);
  }
  const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
  const rows = await db
    .prepare(
      `SELECT id, ts, actor_id, actor_handle, action, target_type, target_id, metadata, ip
         FROM audit_log ${where}
        ORDER BY ts DESC
        LIMIT ?`
    )
    .bind(...vals, limit)
    .all<AuditLogRow>();
  return rows.results ?? [];
}

export async function searchSpecimensFts(
  db: D1Database,
  q: string,
  limit = 24
): Promise<SearchResponse> {
  const matchQuery = toFtsQuery(q);
  if (!matchQuery) {
    return { results: [], mode: "keyword" };
  }

  const rows = await db
    .prepare(
      `SELECT ${SUMMARY_COLUMNS}
       FROM specimens_fts
       JOIN specimens s ON s.rowid = specimens_fts.rowid
       JOIN users_profile u ON u.id = s.author_id
       WHERE specimens_fts MATCH ? AND s.visibility = 'public' AND u.banned = 0
       ORDER BY bm25(specimens_fts), s.likes_count DESC, s.slug ASC
       LIMIT ?`
    )
    .bind(matchQuery, normalizeSearchLimit(limit))
    .all<SpecimenSummaryRow>();

  return {
    results: (rows.results ?? []).map(rowToSummary),
    mode: "keyword"
  };
}

// App-side FTS upsert. specimens_fts has content='' (external-content FTS5), so
// its rows are NOT auto-maintained from the specimens table; we mirror the
// searchable text (title, description, tags, author_handle, dat_text) here. tags
// and dat_text live OUTSIDE the specimens row (specimen_tags / the R2 TDN blob),
// so a pure SQL trigger cannot repopulate them on UPDATE -- this app-side upsert
// is the only place that has them. It is idempotent: INSERT OR REPLACE keyed by
// rowid means re-running it (re-submit, or a future EDIT) overwrites the row in
// place rather than double-inserting. Deletes are handled by the AFTER DELETE ON
// specimens trigger in migration 0005 (which removes the matching FTS row).
//
// FUTURE: a specimen-EDIT path (new version, retitled, retagged) MUST call this
// again with the updated fields so the FTS mirror does not go stale. The DELETE
// trigger covers removal; UPDATE has no SQL trigger and relies on this re-run.
export async function syncSpecimensFts(
  db: D1Database,
  input: {
    specimenId: string;
    slug: string;
    title: string;
    description: string;
    tags: string[];
    authorHandle: string;
    datText?: string;
  }
): Promise<void> {
  const row = await db
    .prepare("SELECT rowid FROM specimens WHERE id = ? LIMIT 1")
    .bind(input.specimenId)
    .first<{ rowid: number }>();

  if (typeof row?.rowid !== "number") {
    throw new Error("Missing specimen rowid for FTS sync.");
  }

  await db
    .prepare(
      `INSERT OR REPLACE INTO specimens_fts
       (rowid, slug, title, description, tags, author_handle, dat_text)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    )
    .bind(
      row.rowid,
      input.slug,
      input.title,
      input.description,
      input.tags.join(" "),
      input.authorHandle,
      input.datText ?? ""
    )
    .run();
}

export function normalizeSpecimenSort(value: string | null): SpecimenSort {
  if (
    value === "updated" ||
    value === "popular" ||
    value === "views" ||
    value === "name"
  ) {
    return value;
  }
  return "recent";
}

export function normalizeCollectionSort(value: string | null | undefined): CollectionSort {
  if (value === "newest" || value === "copied" || value === "liked") return value;
  return "az";
}

// Keyset cursor (de)serialization. The cursor is an opaque base64url token of a
// small JSON object; it is reproducible across requests and never trusted as a
// SQL fragment -- only its decoded { k, slug } values are bound as parameters.
function encodeCollectionCursor(cursor: CollectionCursor): string {
  const json = JSON.stringify([cursor.sort, cursor.k, cursor.slug]);
  return base64UrlEncode(json);
}

function decodeCollectionCursor(
  value: string | undefined,
  sort: CollectionSort
): CollectionCursor | null {
  if (!value) return null;

  try {
    const parsed = JSON.parse(base64UrlDecode(value)) as unknown;
    if (!Array.isArray(parsed) || parsed.length !== 3) return null;

    const [cursorSort, k, slug] = parsed;
    // A cursor minted under a different sort is meaningless against this order;
    // ignore it and serve from the top rather than paginate incoherently.
    if (cursorSort !== sort) return null;
    if (typeof slug !== "string") return null;
    if (typeof k !== "string" && typeof k !== "number") return null;

    return { sort, k, slug };
  } catch {
    return null;
  }
}

function base64UrlEncode(value: string): string {
  // btoa expects Latin-1; the cursor payload is ASCII JSON, so this is safe.
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlDecode(value: string): string {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  return atob(padded);
}

function specimenListQuery(options: ListSpecimensOptions): {
  joins: string;
  where: string[];
  params: (string | number)[];
} {
  // Public + non-banned author (the consuming listSpecimens always joins u).
  const where = ["s.visibility = 'public'", "u.banned = 0"];
  const params: (string | number)[] = [];
  const joins: string[] = [];

  if (options.tag) {
    const tagSlug = slugify(options.tag);
    if (tagSlug) {
      joins.push(
        "JOIN specimen_tags st_filter ON st_filter.specimen_id = s.id",
        "JOIN tags t_filter ON t_filter.id = st_filter.tag_id"
      );
      where.push("t_filter.slug = ?");
      params.push(tagSlug);
    }
  }

  if (options.author) {
    const author = options.author.trim();
    if (author) {
      where.push("u.handle = ?");
      params.push(author);
    }
  }

  return {
    joins: joins.join("\n"),
    where,
    params
  };
}

function rowToSummary(row: SpecimenSummaryRow): SpecimenSummary {
  return {
    slug: row.slug,
    name: row.title,
    category: row.category,
    categories: buildCategories(row.category, row.categories_concat),
    level: normalizeLevel(row.level),
    description: row.description,
    requires: parseRequires(row.requires),
    op_count: Number(row.op_count ?? 0),
    thumbnail_key: row.thumbnail_key ?? "",
    video_key: row.video_key ?? null,
    author_handle: row.author_handle,
    author_avatar_url: row.author_avatar_url ?? null,
    tier: normalizeTier(row.tier),
    visibility: normalizeVisibility(row.visibility),
    likes_count: Number(row.likes_count ?? 0),
    reactions: parseReactions(row.reactions_summary),
    views_count: Number(row.views_count ?? 0),
    copies_count: Number(row.copies_count ?? 0)
  };
}

function normalizePage(value: number | undefined): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.trunc(value ?? 1));
}

function normalizePageSize(value: number | undefined): number {
  if (!Number.isFinite(value)) return DEFAULT_PAGE_SIZE;
  return Math.min(MAX_PAGE_SIZE, Math.max(1, Math.trunc(value ?? DEFAULT_PAGE_SIZE)));
}

function normalizeSearchLimit(value: number): number {
  if (!Number.isFinite(value)) return 24;
  return Math.min(50, Math.max(1, Math.trunc(value)));
}

function normalizeLevel(value: string): Level {
  if (value === "starter" || value === "advanced") return value;
  return "intermediate";
}

// requires is stored as a JSON array of strings. Parse defensively: a legacy
// pre-0009 single value (or the old "none" sentinel) maps to [] or [value]; bad
// JSON yields []. Empty array = stock TouchDesigner.
function parseRequires(raw: string | null): string[] {
  if (!raw) return [];
  const trimmed = raw.trim();
  if (!trimmed || trimmed === "none") return [];
  if (trimmed.startsWith("[")) {
    try {
      const arr = JSON.parse(trimmed);
      if (Array.isArray(arr)) {
        return arr.filter((v): v is string => typeof v === "string" && v.trim().length > 0);
      }
    } catch {
      /* fall through */
    }
    return [];
  }
  return [trimmed]; // legacy single value
}

// Build the primary-first category list for a SpecimenSummary. `primary` is the
// specimens.category column; `concat` is the char(31)-joined full set from the
// specimen_categories join table. Primary leads; the rest follow sorted for a
// stable order. Falls back to [primary] when the join table has no rows yet
// (e.g. a specimen created before the backfill ran).
const CAT_SEP = String.fromCharCode(31);
function buildCategories(primary: string, concat: string | null): string[] {
  const all = (concat ?? "")
    .split(CAT_SEP)
    .map((c) => c.trim())
    .filter(Boolean);
  const set = new Set(all);
  if (primary) set.add(primary);
  const rest = [...set].filter((c) => c !== primary).sort((a, b) => a.localeCompare(b));
  return primary ? [primary, ...rest] : rest;
}

// Normalize a submitted categories list for storage: trim, drop empties, dedupe
// (order-preserving, so the first stays primary), cap at MAX_CATEGORIES. If the
// result is empty, fall back to a single `fallbackPrimary` so every specimen
// always has at least one category. Whitelist validation happens in the route.
function normalizeCategories(input: string[] | undefined, fallbackPrimary: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of input ?? []) {
    const t = (v ?? "").trim();
    if (!t || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
    if (out.length >= MAX_CATEGORIES) break;
  }
  if (out.length === 0 && fallbackPrimary) out.push(fallbackPrimary);
  return out;
}

// Normalize a submitted requires list into the JSON string stored in D1: trim,
// drop empties and the "none" sentinel, dedupe (order-preserving).
function serializeRequires(input: string[] | undefined): string {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of input ?? []) {
    const t = (v ?? "").trim();
    if (!t || t === "none" || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
  }
  return JSON.stringify(out);
}

function normalizeTier(value: string): Tier {
  if (value === "verified" || value === "featured") return value;
  return "community";
}

function normalizeVisibility(value: string): Visibility {
  if (value === "private" || value === "unlisted") return value;
  return "public";
}

function normalizeTrustLevel(value: string): TrustLevel {
  if (value === "verified" || value === "curator" || value === "admin") return value;
  return "anon";
}

function parseCapability(value: string | null): CapabilityJson {
  if (!value) return emptyCapability();

  try {
    const parsed = JSON.parse(value) as Partial<CapabilityJson>;
    if (
      parsed &&
      typeof parsed.scanner_version === "string" &&
      (parsed.verdict === "clean" || parsed.verdict === "flagged" || parsed.verdict === "blocked") &&
      parsed.counts &&
      Array.isArray(parsed.findings)
    ) {
      return parsed as CapabilityJson;
    }
  } catch {
    return emptyCapability();
  }

  return emptyCapability();
}

function emptyCapability(): CapabilityJson {
  return {
    scanner_version: "unknown",
    verdict: "clean",
    counts: emptyCapabilityCounts(),
    findings: []
  };
}

async function uniqueSlug(db: D1Database, title: string): Promise<string> {
  const base = slugify(title) || "specimen";
  let slug = base;

  for (let attempt = 0; attempt < 8; attempt += 1) {
    const existing = await db
      .prepare("SELECT slug FROM specimens WHERE slug = ? LIMIT 1")
      .bind(slug)
      .first<{ slug: string }>();

    if (!existing) return slug;

    slug = `${base}-${shortId()}`;
  }

  return `${base}-${crypto.randomUUID().slice(0, 8)}`;
}

function normalizeTags(values: string[]): { name: string; slug: string }[] {
  const tags: { name: string; slug: string }[] = [];
  const seen = new Set<string>();

  for (const value of values.slice(0, 20)) {
    const name = value.trim().slice(0, 80);
    const slug = slugify(name);
    if (!name || !slug || seen.has(slug)) continue;
    seen.add(slug);
    tags.push({ name, slug });
  }

  return tags;
}

function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function shortId(): string {
  const bytes = new Uint8Array(4);
  crypto.getRandomValues(bytes);
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function countTdnOperators(value: unknown): number {
  if (!isRecord(value)) return 0;

  let count = hasOperatorShape(value) ? 1 : 0;
  for (const key of ["operators", "children"] as const) {
    const children = value[key];
    if (!Array.isArray(children)) continue;

    for (const child of children) {
      count += countTdnOperators(child);
    }
  }

  return count;
}

function extractDatText(value: unknown, limit = 20000): string {
  const chunks: string[] = [];
  collectDatText(value, chunks, limit);
  return chunks.join("\n").slice(0, limit);
}

function collectDatText(value: unknown, chunks: string[], limit: number): void {
  if (!isRecord(value) || chunks.join("\n").length >= limit) return;

  if (typeof value.dat_content === "string" && value.dat_content.trim()) {
    chunks.push(value.dat_content.slice(0, limit));
  }

  for (const key of ["operators", "children"] as const) {
    const children = value[key];
    if (!Array.isArray(children)) continue;

    for (const child of children) {
      collectDatText(child, chunks, limit);
    }
  }
}

function hasOperatorShape(value: Record<string, unknown>): boolean {
  return typeof value.name === "string" || typeof value.type === "string";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toFtsQuery(value: string): string {
  const terms = value
    .toLowerCase()
    .match(/[a-z0-9_]+/g)
    ?.slice(0, 8);

  if (!terms?.length) return "";
  return terms.map((term) => `${term}*`).join(" AND ");
}

// ---------------------------------------------------------------------------
// Admin panel queries. Read/write helpers for the moderation panel. Admin reads
// intentionally do NOT filter visibility = 'public' -- an admin sees everything.
// Every mutating helper returns whether a row actually changed. Enum inputs
// (status/visibility/tier/trust_level) are validated by the API route before
// these run (src/server/admin.ts); values are always bound, never interpolated.
// ---------------------------------------------------------------------------

export interface DashboardCounts {
  openReports: number;
  totalSpecimens: number;
  totalUsers: number;
  recentSignups: number; // users_profile rows created in the last 7 days
}

export async function dashboardCounts(db: D1Database): Promise<DashboardCounts> {
  const row = await db
    .prepare(
      `SELECT
         (SELECT COUNT(*) FROM reports WHERE status = 'open') AS openReports,
         (SELECT COUNT(*) FROM specimens) AS totalSpecimens,
         (SELECT COUNT(*) FROM users_profile) AS totalUsers,
         (SELECT COUNT(*) FROM users_profile WHERE created_at >= datetime('now','-7 days')) AS recentSignups`
    )
    .first<{ openReports: number; totalSpecimens: number; totalUsers: number; recentSignups: number }>();
  return {
    openReports: Number(row?.openReports ?? 0),
    totalSpecimens: Number(row?.totalSpecimens ?? 0),
    totalUsers: Number(row?.totalUsers ?? 0),
    recentSignups: Number(row?.recentSignups ?? 0)
  };
}

export interface AdminReportRow {
  id: string;
  status: string;
  reason: string;
  details: string | null;
  created_at: string;
  specimen_id: string | null;
  specimen_slug: string | null;
  specimen_title: string | null;
  specimen_visibility: string | null;
  reporter_handle: string | null;
  author_id: string | null;
  author_handle: string | null;
  author_banned: number | null;
}

// Moderation queue. LEFT JOINs so an orphaned report (specimen/reporter deleted)
// still lists. Newest first. Optional status filter.
export async function listReports(
  db: D1Database,
  opts: { status?: string; limit?: number } = {}
): Promise<AdminReportRow[]> {
  const limit = Math.min(Math.max(opts.limit ?? 100, 1), 200);
  const where = opts.status ? "WHERE r.status = ?" : "";
  const stmt = db.prepare(
    `SELECT r.id, r.status, r.reason, r.details, r.created_at,
            s.id AS specimen_id, s.slug AS specimen_slug, s.title AS specimen_title,
            s.visibility AS specimen_visibility,
            u.handle AS reporter_handle,
            s.author_id AS author_id, au.handle AS author_handle, au.banned AS author_banned
       FROM reports r
       LEFT JOIN specimens s ON s.id = r.specimen_id
       LEFT JOIN users_profile u ON u.id = r.reporter_id
       LEFT JOIN users_profile au ON au.id = s.author_id
       ${where}
      ORDER BY r.created_at DESC
      LIMIT ?`
  );
  const bound = opts.status ? stmt.bind(opts.status, limit) : stmt.bind(limit);
  const rows = await bound.all<AdminReportRow>();
  return rows.results ?? [];
}

export async function updateReportStatus(
  db: D1Database,
  reportId: string,
  status: string
): Promise<boolean> {
  const res = await db
    .prepare("UPDATE reports SET status = ? WHERE id = ?")
    .bind(status, reportId)
    .run();
  return Number(res.meta?.changes ?? 0) > 0;
}

export interface AdminSpecimenRow {
  id: string;
  slug: string;
  title: string;
  author_handle: string | null;
  visibility: string;
  tier: string;
  scan_status: string;
  likes_count: number;
  views_count: number;
  created_at: string;
}

// Specimen list for the admin table -- ALL visibilities. Optional title/slug
// substring filter.
export async function listSpecimensForAdmin(
  db: D1Database,
  opts: { q?: string; limit?: number } = {}
): Promise<AdminSpecimenRow[]> {
  const limit = Math.min(Math.max(opts.limit ?? 100, 1), 200);
  const q = (opts.q ?? "").trim();
  const where = q ? "WHERE s.title LIKE ? OR s.slug LIKE ?" : "";
  const stmt = db.prepare(
    `SELECT s.id, s.slug, s.title, u.handle AS author_handle,
            s.visibility, s.tier, s.scan_status, s.likes_count, s.views_count, s.created_at
       FROM specimens s
       LEFT JOIN users_profile u ON u.id = s.author_id
       ${where}
      ORDER BY s.created_at DESC
      LIMIT ?`
  );
  const like = `%${q}%`;
  const bound = q ? stmt.bind(like, like, limit) : stmt.bind(limit);
  const rows = await bound.all<AdminSpecimenRow>();
  return rows.results ?? [];
}

// Dynamic SET of only the provided moderation fields (+ updated_at). Returns
// false when nothing was provided or no row matched.
export async function updateSpecimenModeration(
  db: D1Database,
  specimenId: string,
  patch: { visibility?: string; tier?: string }
): Promise<boolean> {
  const sets: string[] = [];
  const vals: string[] = [];
  if (patch.visibility !== undefined) {
    sets.push("visibility = ?");
    vals.push(patch.visibility);
  }
  if (patch.tier !== undefined) {
    sets.push("tier = ?");
    vals.push(patch.tier);
  }
  if (!sets.length) return false;
  sets.push("updated_at = datetime('now')");
  const res = await db
    .prepare(`UPDATE specimens SET ${sets.join(", ")} WHERE id = ?`)
    .bind(...vals, specimenId)
    .run();
  return Number(res.meta?.changes ?? 0) > 0;
}

export interface AdminUserRow {
  id: string;
  handle: string;
  email: string | null;
  email_verified: number | null;
  trust_level: string;
  created_at: string;
  banned: number;
  banned_reason: string | null;
}

// User list for the admin table. Joins the app profile to the Better Auth `user`
// table for email + verification status ("user" is quoted -- it's a Better Auth
// table name, created by its own migration). Optional handle/email filter.
export async function listUsersForAdmin(
  db: D1Database,
  opts: { q?: string; limit?: number } = {}
): Promise<AdminUserRow[]> {
  const limit = Math.min(Math.max(opts.limit ?? 100, 1), 200);
  const q = (opts.q ?? "").trim();
  const where = q ? "WHERE p.handle LIKE ? OR u.email LIKE ?" : "";
  const stmt = db.prepare(
    `SELECT p.id, p.handle, u.email AS email, u.emailVerified AS email_verified,
            p.trust_level, p.created_at, p.banned, p.banned_reason
       FROM users_profile p
       LEFT JOIN "user" u ON u.id = p.id
       ${where}
      ORDER BY p.created_at DESC
      LIMIT ?`
  );
  const like = `%${q}%`;
  const bound = q ? stmt.bind(like, like, limit) : stmt.bind(limit);
  const rows = await bound.all<AdminUserRow>();
  return rows.results ?? [];
}

export async function setUserTrustLevel(
  db: D1Database,
  userId: string,
  trustLevel: string
): Promise<boolean> {
  const res = await db
    .prepare("UPDATE users_profile SET trust_level = ? WHERE id = ?")
    .bind(trustLevel, userId)
    .run();
  return Number(res.meta?.changes ?? 0) > 0;
}

// The R2 key of a public specimen's author-uploaded thumbnail, or null when it
// has none. Backs GET /api/specimens/:slug/thumbnail.
export async function getThumbnailKeyForSlug(
  db: D1Database,
  slug: string,
  // When set to the signed-in user's id, that user can also resolve the thumbnail
  // of their OWN non-public (private/unlisted) specimen -- so an author viewing a
  // draft sees its cover, matching getSpecimenBySlug's visibility rule. Without it
  // (anonymous / other viewer), only public specimens resolve.
  viewerId?: string | null
): Promise<string | null> {
  const row = await db
    .prepare(
      `SELECT thumbnail_key FROM specimens
        WHERE slug = ? AND (visibility = 'public' OR author_id = ?)
          AND author_id NOT IN (SELECT id FROM users_profile WHERE banned = 1)
        LIMIT 1`
    )
    .bind(slug, viewerId ?? "")
    .first<{ thumbnail_key: string | null }>();
  const key = row?.thumbnail_key ?? "";
  return key ? key : null;
}

// The R2 key of a public specimen's author-uploaded cover video, or null when it
// has none. Backs GET /api/specimens/:slug/video.
export async function getVideoKeyForSlug(
  db: D1Database,
  slug: string,
  // When set to the signed-in user's id, that user can also resolve the video of
  // their OWN non-public (private/unlisted) specimen -- so an author viewing a
  // draft sees its cover, matching getSpecimenBySlug's visibility rule. Without it
  // (anonymous / other viewer), only public specimens resolve.
  viewerId?: string | null
): Promise<string | null> {
  const row = await db
    .prepare(
      `SELECT video_key FROM specimens
        WHERE slug = ? AND (visibility = 'public' OR author_id = ?)
          AND author_id NOT IN (SELECT id FROM users_profile WHERE banned = 1)
        LIMIT 1`
    )
    .bind(slug, viewerId ?? "")
    .first<{ video_key: string | null }>();
  const key = row?.video_key ?? "";
  return key ? key : null;
}
