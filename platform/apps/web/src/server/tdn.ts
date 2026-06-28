import { parse } from "yaml";
import { getCurrentTdnBlobForSlug } from "./db";
import { getTdn } from "./r2";

// The .tdn blobs stored in R2 are TDN v2.0 YAML (Embody's on-disk format).
// This helper resolves a slug to its current-version blob, downloads the raw
// YAML text from R2, and parses it into a TDN object. The Cloudflare Workers
// runtime + the @astrojs/cloudflare adapter support the pure-JS 'yaml' parser.

export interface ParsedTdn {
  /** R2 key (= sha256 of the .tdn bytes). */
  key: string;
  /** Raw .tdn YAML text exactly as stored in R2. */
  raw: string;
  /** Parsed TDN network dict. */
  tdn: Record<string, unknown>;
}

/** Parse raw TDN v2.0 YAML text into a TDN object, or null if it is not a map. */
export function parseTdnYaml(raw: string | null): Record<string, unknown> | null {
  if (!raw) return null;

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
 * Resolve a slug to its current TDN blob: fetch the YAML text from R2 and parse
 * it. Returns null when the specimen, its blob, or a valid parse is missing.
 */
export async function getParsedTdnForSlug(
  db: D1Database,
  blobs: R2Bucket,
  slug: string,
  // Forwarded to getCurrentTdnBlobForSlug: when set to the signed-in user's id,
  // the author can resolve their OWN private draft's network (for the specimen
  // page preview / edit prefill). Unset = public only (the public /tdn + /copy).
  viewerId?: string | null
): Promise<ParsedTdn | null> {
  const blob = await getCurrentTdnBlobForSlug(db, slug, viewerId);
  if (!blob) return null;

  const raw = await getTdn(blobs, blob.key);
  if (raw === null) return null;

  const tdn = parseTdnYaml(raw);
  if (!tdn) return null;

  return { key: blob.key, raw, tdn };
}
