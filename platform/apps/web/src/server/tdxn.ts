import { parse } from "yaml";
import { getCurrentTdxnBlobForSlug } from "./db";
import { getTdxn } from "./r2";

// The .tdn blobs stored in R2 are TDXN v2.0 YAML (Embody's on-disk format).
// This helper resolves a slug to its current-version blob, downloads the raw
// YAML text from R2, and parses it into a TDXN object. The Cloudflare Workers
// runtime + the @astrojs/cloudflare adapter support the pure-JS 'yaml' parser.

export interface ParsedTdxn {
  /** R2 key (= sha256 of the .tdn bytes). */
  key: string;
  /** Raw .tdn YAML text exactly as stored in R2. */
  raw: string;
  /** Parsed TDXN network dict. */
  tdxn: Record<string, unknown>;
}

// Hard cap on the raw YAML text we will hand to the parser, measured in JS
// string length (UTF-16 code units -- a tight upper bound on parse cost). parse()
// is a synchronous, CPU-and-memory-bound operation on the Worker's main thread;
// an oversized or pathologically nested document can exhaust the 128MB isolate or
// burn the CPU budget (DoS). Reject anything past the cap up front. The 'yaml'
// library already bounds alias expansion (maxAliasCount, default 100), so this
// guards the remaining size/nesting vector. 8M chars comfortably exceeds any real
// specimen while staying well under the isolate limit. Exported so the submit/
// edit WRITE paths (which run their own parse) enforce the SAME bound -- the read
// helper below covers /tdn, /copy, /c/[slug], cover-graph.
export const MAX_TDXN_TEXT_CHARS = 8 * 1024 * 1024;

/** Parse raw TDXN v2.0 YAML text into a TDXN object, or null if it is not a map. */
export function parseTdxnYaml(raw: string | null): Record<string, unknown> | null {
  if (!raw) return null;

  if (raw.length > MAX_TDXN_TEXT_CHARS) {
    console.error(
      `parseTdxnYaml: raw TDXN is ${raw.length} chars, over the ${MAX_TDXN_TEXT_CHARS} cap -- refusing to parse`
    );
    return null;
  }

  try {
    const parsed = parse(raw) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }

  return null;
}

/**
 * Resolve a slug to its current TDXN blob: fetch the YAML text from R2 and parse
 * it. Returns null when the specimen, its blob, or a valid parse is missing.
 */
export async function getParsedTdxnForSlug(
  db: D1Database,
  blobs: R2Bucket,
  slug: string,
  // Forwarded to getCurrentTdxnBlobForSlug: when set to the signed-in user's id,
  // the author can resolve their OWN private draft's network (for the specimen
  // page preview / edit prefill). Unset = public only (the public /tdn + /copy).
  viewerId?: string | null
): Promise<ParsedTdxn | null> {
  const blob = await getCurrentTdxnBlobForSlug(db, slug, viewerId);
  if (!blob) return null;

  const raw = await getTdxn(blobs, blob.key);
  if (raw === null) return null;

  const tdxn = parseTdxnYaml(raw);
  if (!tdxn) return null;

  return { key: blob.key, raw, tdxn };
}
