import { env } from "cloudflare:workers";
import { detectObviousMalware, scanTdn } from "@embody/scanner-ts";
import { parse as parseYaml } from "yaml";
import {
  SUBMIT_CATEGORIES,
  SUBMIT_DIFFICULTIES,
  SUBMIT_REQUIRES,
  type Difficulty,
  type SubmitRequest,
  type SubmitResponse
} from "@embody/contracts";
import type { APIRoute } from "astro";
import {
  insertSpecimenWithVersion,
  listSpecimensForCollection,
  normalizeCollectionSort
} from "../../../server/db";
import { requireUser } from "../../../server/auth";
import { notifyOwnerNewSpecimen } from "../../../server/notifications";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";
import { byteLength, putTdn, putThumbnail } from "../../../server/r2";
import { checkRateLimit } from "../../../server/rateLimit";
import { verifyTurnstile } from "../../../server/turnstile";

// Submit abuse cap: 10 submissions per 10 minutes per client IP. Enforced via
// KV; with no KV (dev) the limiter allows everything (see rateLimit.ts).
const SUBMIT_RATE_LIMIT = { limit: 10, windowSec: 600 } as const;

export const prerender = false;

// Server-side list/filter/sort/paginate for the collection page. Accepts:
//   q          full-text query (FTS via specimens_fts)
//   category   exact category facet
//   difficulty starter | intermediate | advanced
//   requires   exact requires facet ("none", "MediaPipe", ...)
//   sort       newest | copied | az (default az)
//   cursor     opaque keyset cursor from a prior page's nextCursor
//   pageSize   default 24, max 100
// Returns { specimens, count, page, pageSize, nextCursor }; cost is O(pageSize).
export const GET: APIRoute = async ({ url }) => {
  try {
    const params = url.searchParams;
    const response = await listSpecimensForCollection(env.DB, {
      q: params.get("q") ?? undefined,
      category: params.get("category") ?? undefined,
      difficulty: params.get("difficulty") ?? undefined,
      requires: params.get("requires") ?? undefined,
      sort: normalizeCollectionSort(params.get("sort")),
      cursor: params.get("cursor") ?? undefined,
      pageSize: parsePositiveInteger(params.get("pageSize"))
    });

    return jsonResponse(response, {
      headers: {
        "Cache-Control": "public, max-age=60, s-maxage=300"
      }
    });
  } catch (error) {
    console.error("GET /api/specimens failed", error);
    return serverErrorResponse();
  }
};

export const POST: APIRoute = async ({ request }) => {
  try {
    // Per-IP fixed-window cap before any expensive work (parse/scan/R2/D1). The
    // CF-Connecting-IP header is set by Cloudflare's edge; "unknown" buckets
    // callers we can't identify together. No KV (dev) -> always allowed.
    const clientIp = request.headers.get("CF-Connecting-IP") ?? "unknown";
    const rate = await checkRateLimit(env.KV, `submit:${clientIp}`, SUBMIT_RATE_LIMIT);
    if (!rate.ok) {
      return errorResponse(
        429,
        "rate_limited",
        "Too many submissions. Please slow down and try again shortly.",
        rate.retryAfter ? { "Retry-After": String(rate.retryAfter) } : undefined
      );
    }

    const body = await readSubmitRequest(request);
    if (!body.ok) {
      return errorResponse(400, "invalid_request", body.detail);
    }

    let user;
    try {
      user = await requireUser(request, env);
    } catch {
      return errorResponse(401, "authentication_required", "A signed-in user is required.");
    }

    const turnstileOk = await verifyTurnstile(
      body.request.turnstileToken,
      env.TURNSTILE_SECRET,
      env.ENVIRONMENT
    );
    if (!turnstileOk) {
      return errorResponse(403, "turnstile_failed", "Turnstile verification failed.");
    }

    const parsedTdn = parseTdn(body.request.tdn);
    if (!parsedTdn.ok) {
      return errorResponse(400, "invalid_tdn", parsedTdn.detail);
    }

    const scan = scanTdn(parsedTdn.tdn);
    if (scan.verdict === "blocked") {
      return jsonResponse(
        {
          error: "scan_blocked",
          detail: "The submitted TDN includes blocked capability surfaces.",
          scan
        },
        { status: 422 }
      );
    }

    // Submit-side hard-block: reject ONLY unambiguous malware (droppers / shell-network-exec /
    // reverse shells). Generic executable surfaces stay flagged-and-accepted (default-inert import).
    const malware = detectObviousMalware(parsedTdn.tdn);
    if (malware.malicious) {
      return jsonResponse(
        {
          error: "rejected_malware",
          detail: "Submission contains an unambiguously malicious pattern and was rejected.",
          reasons: malware.reasons,
          scan
        },
        { status: 422 }
      );
    }

    const blob = await putTdn(env.BLOBS, body.request.tdn);
    const thumbnail = await putThumbnail(env.BLOBS, body.request.thumbnail);
    const inserted = await insertSpecimenWithVersion(env.DB, {
      user,
      title: body.request.title,
      description: body.request.description,
      tags: body.request.tags,
      license: body.request.license,
      difficulty: body.request.difficulty,
      category: body.request.category,
      requires: body.request.requires,
      tdnR2Key: blob.key,
      tdnSha256: blob.sha256,
      sizeBytes: byteLength(body.request.tdn),
      scan,
      thumbnailKey: thumbnail?.key,
      parsedTdn: parsedTdn.tdn
    });

    // Operational notice to the owner that public content went live. Self-
    // swallowing + safe-by-default (notifications.ts), so it never affects the
    // 201 the submitter receives.
    await notifyOwnerNewSpecimen(env as CloudflareEnv, {
      title: body.request.title,
      slug: inserted.slug,
      handle: user.handle,
      scanVerdict: scan.verdict
    });

    const response: SubmitResponse = {
      slug: inserted.slug,
      scan
    };

    return jsonResponse(response, {
      status: 201,
      headers: {
        Location: `/api/specimens/${inserted.slug}`
      }
    });
  } catch {
    return serverErrorResponse();
  }
};

function parsePositiveInteger(value: string | null): number | undefined {
  if (!value) return undefined;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
}

async function readSubmitRequest(
  request: Request
): Promise<{ ok: true; request: SubmitRequest } | { ok: false; detail: string }> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return { ok: false, detail: "Request body must be valid JSON." };
  }

  if (!isRecord(raw)) {
    return { ok: false, detail: "Request body must be a JSON object." };
  }

  const title = readString(raw.title).trim();
  const description = readString(raw.description).trim();
  const license = readString(raw.license).trim() || "CC-BY-4.0";
  const tdn = readString(raw.tdn);
  const turnstileToken = readString(raw.turnstileToken);
  const tags = Array.isArray(raw.tags)
    ? raw.tags
        .filter((tag): tag is string => typeof tag === "string")
        .map((tag) => tag.trim())
        .filter(Boolean)
        .slice(0, 20)
    : [];

  if (!title) return { ok: false, detail: "title is required." };
  if (!tdn) return { ok: false, detail: "tdn is required." };
  if (!turnstileToken) return { ok: false, detail: "turnstileToken is required." };

  // Submit metadata: whitelist-validate against the frozen vocabularies. Each
  // defaults to a safe value when absent (back-compat with older callers), but
  // a PRESENT value outside its set is a hard 400 rather than a silent coerce.
  const difficulty = readString(raw.difficulty).trim() || "intermediate";
  if (!SUBMIT_DIFFICULTIES.includes(difficulty as Difficulty)) {
    return {
      ok: false,
      detail: `difficulty must be one of: ${SUBMIT_DIFFICULTIES.join(", ")}.`
    };
  }

  const category = readString(raw.category).trim();
  if (category && !SUBMIT_CATEGORIES.includes(category)) {
    return {
      ok: false,
      detail: `category must be one of: ${SUBMIT_CATEGORIES.join(", ")}.`
    };
  }

  const requires = readString(raw.requires).trim() || "none";
  if (!SUBMIT_REQUIRES.includes(requires)) {
    return {
      ok: false,
      detail: `requires must be one of: ${SUBMIT_REQUIRES.join(", ")}.`
    };
  }

  const thumbnail = typeof raw.thumbnail === "string" ? raw.thumbnail : undefined;

  return {
    ok: true,
    request: {
      title: title.slice(0, 160),
      description: description.slice(0, 4000),
      tags,
      license: license.slice(0, 80),
      difficulty: difficulty as Difficulty,
      category,
      requires,
      tdn,
      thumbnail,
      turnstileToken
    }
  };
}

// TDN is YAML v2.0 (a strict JSON superset, so legacy JSON still parses).
function parseTdn(
  value: string
): { ok: true; tdn: Record<string, unknown> } | { ok: false; detail: string } {
  let parsed: unknown;
  try {
    parsed = parseYaml(value) as unknown;
  } catch {
    return { ok: false, detail: "tdn must be valid YAML or JSON." };
  }
  if (!isRecord(parsed)) {
    return { ok: false, detail: "tdn must parse to a mapping (object)." };
  }
  return { ok: true, tdn: parsed };
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
