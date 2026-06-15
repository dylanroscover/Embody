import {
  emptyCapabilityCounts,
  type CapabilityJson,
  type Difficulty,
  type ListResponse,
  type SearchResponse,
  type SpecimenDetail,
  type SpecimenSummary,
  type Tier
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
  difficulty?: string;
  requires?: string;
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
  difficulty?: Difficulty;
  /** Known category facet; empty falls back to the first tag (legacy behavior). */
  category?: string;
  /** Hardware/capability requirement ("none" = stock TD). */
  requires?: string;
  tdnR2Key: string;
  tdnSha256: string;
  sizeBytes: number;
  scan: CapabilityJson;
  thumbnailKey?: string;
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
  difficulty: string;
  description: string;
  requires: string;
  op_count: number;
  thumbnail_key: string | null;
  author_handle: string;
  tier: string;
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
  "s.difficulty",
  "s.description",
  "s.requires",
  "s.op_count",
  "s.thumbnail_key",
  "u.handle AS author_handle",
  "s.tier",
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

  const where = ["s.visibility = 'public'"];
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

  const category = (options.category ?? "").trim();
  if (category) {
    where.push("s.category = ?");
    filterParams.push(category);
  }

  const difficulty = (options.difficulty ?? "").trim();
  if (difficulty) {
    where.push("s.difficulty = ?");
    filterParams.push(difficulty);
  }

  const requires = (options.requires ?? "").trim();
  if (requires) {
    where.push("s.requires = ?");
    filterParams.push(requires);
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
}

export async function getCollectionFacets(db: D1Database): Promise<CollectionFacets> {
  const categories = await db
    .prepare(
      `SELECT DISTINCT category AS value
       FROM specimens
       WHERE visibility = 'public' AND category <> ''
       ORDER BY category COLLATE NOCASE ASC`
    )
    .all<{ value: string }>();

  const requires = await db
    .prepare(
      `SELECT DISTINCT requires AS value
       FROM specimens
       WHERE visibility = 'public' AND requires <> ''
       ORDER BY requires COLLATE NOCASE ASC`
    )
    .all<{ value: string }>();

  return {
    categories: (categories.results ?? []).map((row) => row.value),
    requires: (requires.results ?? []).map((row) => row.value)
  };
}

export async function getSpecimenBySlug(
  db: D1Database,
  slug: string
): Promise<SpecimenDetail | null> {
  // Tags are folded into the detail row via a correlated GROUP_CONCAT subquery
  // (delimited by char(31), the ASCII unit separator, so a comma inside a tag
  // name can't corrupt the split), collapsing the prior 2-query N+1 into one
  // round trip. The inner ORDER BY in the derived table feeds GROUP_CONCAT its
  // rows in case-insensitive name order, matching the old separate query.
  const row = await db
    .prepare(
      `SELECT ${SUMMARY_COLUMNS},
              s.capability_json,
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
       WHERE s.slug = ? AND s.visibility = 'public'
       LIMIT 1`
    )
    .bind(slug)
    .first<SpecimenDetailRow & { tag_names: string | null }>();

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
    capability: parseCapability(row.capability_json),
    current_version: Number(row.current_version ?? 1),
    tags,
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
        `SELECT handle, avatar_url, bio, trust_level, created_at
         FROM users_profile
         WHERE handle = ?
         LIMIT 1`
      )
      .bind(trimmed)
      .first<{
        handle: string;
        avatar_url: string | null;
        bio: string | null;
        trust_level: string;
        created_at: string;
      }>();

    if (row) {
      return {
        handle: row.handle,
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

export async function listSpecimensByAuthor(
  db: D1Database,
  handle: string
): Promise<SpecimenSummary[]> {
  const trimmed = handle.trim();
  if (!trimmed) return [];

  try {
    const rows = await db
      .prepare(
        `SELECT ${SUMMARY_COLUMNS}
         FROM specimens s
         JOIN users_profile u ON u.id = s.author_id
         WHERE u.handle = ? AND s.visibility = 'public'
         ORDER BY s.created_at DESC, s.slug ASC`
      )
      .bind(trimmed)
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
    avatar_url: null,
    bio: "First-party Embody specimen author. Curating the transparent TDN Collection.",
    trust_level: "curator",
    created_at: "2026-01-01T00:00:00Z"
  };
}

function fallbackSpecimensByAuthor(handle: string): SpecimenSummary[] {
  return fixtureSummaries.filter((summary) => summary.author_handle === handle);
}

export async function getCurrentTdnBlobForSlug(
  db: D1Database,
  slug: string
): Promise<CurrentTdnBlob | null> {
  const row = await db
    .prepare(
      `SELECT v.tdn_r2_key AS key,
              s.capability_json
       FROM specimens s
       JOIN specimen_versions v ON v.id = s.current_version_id
       WHERE s.slug = ? AND s.visibility = 'public'
       LIMIT 1`
    )
    .bind(slug)
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
  // Prefer the submit-form category (whitelist-validated upstream); fall back to
  // the first tag's slug, then "community", to preserve pre-metadata behavior.
  const category = (input.category ?? "").trim() || tags[0]?.slug || "community";
  const difficulty = normalizeDifficulty(input.difficulty ?? "intermediate");
  const requires = (input.requires ?? "").trim() || "none";
  const scanStatus = input.scan.verdict;
  const license = input.license.trim() || "CC-BY-4.0";

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
           id, slug, author_id, title, description, category, difficulty, requires,
           op_count, family_summary, current_version_id, thumbnail_key, license,
           visibility, tier, scan_status, capability_json
         )
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'public',
                 'community', ?, ?)`
      )
      .bind(
        specimenId,
        slug,
        input.user.id,
        title,
        description,
        category,
        difficulty,
        requires,
        opCount,
        versionId,
        input.thumbnailKey ?? "",
        license,
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
       WHERE specimens_fts MATCH ? AND s.visibility = 'public'
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
  const where = ["s.visibility = 'public'"];
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
    difficulty: normalizeDifficulty(row.difficulty),
    description: row.description,
    requires: row.requires,
    op_count: Number(row.op_count ?? 0),
    thumbnail_key: row.thumbnail_key ?? "",
    author_handle: row.author_handle,
    tier: normalizeTier(row.tier),
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

function normalizeDifficulty(value: string): Difficulty {
  if (value === "starter" || value === "advanced") return value;
  return "intermediate";
}

function normalizeTier(value: string): Tier {
  if (value === "verified" || value === "featured") return value;
  return "community";
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
