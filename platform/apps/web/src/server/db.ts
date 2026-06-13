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

export type SpecimenSort = "recent" | "updated" | "popular" | "views" | "name";

export interface ListSpecimensOptions {
  sort?: SpecimenSort;
  tag?: string;
  author?: string;
  page?: number;
  pageSize?: number;
}

export interface InsertSpecimenInput {
  user: RequestUser;
  title: string;
  description: string;
  tags: string[];
  license: string;
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
  views_count: number;
}

interface SpecimenDetailRow extends SpecimenSummaryRow {
  capability_json: string | null;
  current_version: number | null;
  created_at: string;
  updated_at: string;
}

interface TagRow {
  name: string;
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
  "s.views_count"
].join(", ");

const SORT_SQL: Record<SpecimenSort, string> = {
  recent: "s.created_at DESC",
  updated: "s.updated_at DESC",
  popular: "s.likes_count DESC",
  views: "s.views_count DESC",
  name: "s.title COLLATE NOCASE ASC"
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

export async function getSpecimenBySlug(
  db: D1Database,
  slug: string
): Promise<SpecimenDetail | null> {
  const row = await db
    .prepare(
      `SELECT ${SUMMARY_COLUMNS},
              s.capability_json,
              v.version_num AS current_version,
              s.created_at,
              s.updated_at
       FROM specimens s
       JOIN users_profile u ON u.id = s.author_id
       LEFT JOIN specimen_versions v ON v.id = s.current_version_id
       WHERE s.slug = ? AND s.visibility = 'public'
       LIMIT 1`
    )
    .bind(slug)
    .first<SpecimenDetailRow>();

  if (!row) return null;

  const tagRows = await db
    .prepare(
      `SELECT t.name
       FROM tags t
       JOIN specimen_tags st ON st.tag_id = t.id
       JOIN specimens s ON s.id = st.specimen_id
       WHERE s.slug = ?
       ORDER BY t.name COLLATE NOCASE ASC`
    )
    .bind(slug)
    .all<TagRow>();

  return {
    ...rowToSummary(row),
    capability: parseCapability(row.capability_json),
    current_version: Number(row.current_version ?? 1),
    tags: (tagRows.results ?? []).map((tag: TagRow) => tag.name),
    created_at: row.created_at,
    updated_at: row.updated_at
  };
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
  const category = tags[0]?.slug ?? "community";
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
         VALUES (?, ?, ?, ?, ?, ?, 'intermediate', 'none', ?, NULL, ?, ?, ?, 'public',
                 'community', ?, ?)`
      )
      .bind(
        specimenId,
        slug,
        input.user.id,
        title,
        description,
        category,
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
    views_count: Number(row.views_count ?? 0)
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
