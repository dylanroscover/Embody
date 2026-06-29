import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getSpecimenBySlug } from "../../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";
import { getParsedTdnForSlug } from "../../../../server/tdn";
import { buildEmbodyEnvelope } from "../../../../lib/tdnEnvelope";
import { checkRateLimit } from "../../../../server/rateLimit";

export const prerender = false;

// Per-IP cap on the unauthenticated copy action. The Idempotency-Key path only
// dedups a single client's retries; it does nothing against a script that omits
// the header, so this is the real bound on copies_count inflation.
const COPY_RATE_LIMIT = { limit: 40, windowSec: 60 };

// POST /api/specimens/:slug/copy
// Builds the `_embody_tdn` clipboard envelope for a specimen from its real R2
// TDN blob, increments the copies_count counter, and returns both. The envelope
// source is "embody.tools" (community provenance) so the Embody TD side imports
// it default-inert (sandboxed paste). Returns { copies_count, envelope }.
// Short TTL for the idempotency marker: long enough to absorb client retries of
// a single copy action, short enough that the key space stays tiny.
const IDEMPOTENCY_TTL_SEC = 600;

export const POST: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
    const rate = await checkRateLimit(env.KV, `copy:${ip}`, COPY_RATE_LIMIT);
    if (!rate.ok) {
      return errorResponse(
        429,
        "rate_limited",
        "Too many copies. Please slow down and try again shortly.",
        rate.retryAfter ? { "Retry-After": String(rate.retryAfter) } : undefined
      );
    }

    const specimen = await getSpecimenBySlug(env.DB, slug);
    if (!specimen) {
      return errorResponse(404, "specimen_not_found", "No specimen exists for that slug.");
    }

    const parsed = await getParsedTdnForSlug(env.DB, env.BLOBS, slug);
    if (!parsed) {
      return errorResponse(404, "tdn_not_found", "The TDN blob is missing or unparseable.");
    }

    const envelope = await buildEmbodyEnvelope(parsed.tdn, {
      slug,
      version: specimen.current_version
    });

    // Idempotency: when the client sends an Idempotency-Key AND KV is available,
    // a retry with the same key must NOT increment copies_count again. We record
    // the count produced by the first successful increment under that key and
    // replay it. No header or no KV -> the original increment-every-time path.
    const idempotencyKey = request.headers.get("Idempotency-Key")?.trim();
    const kv = env.KV;
    const idemStorageKey =
      idempotencyKey && kv ? `copy:${slug}:${idempotencyKey}` : null;

    if (idemStorageKey && kv) {
      let priorCount: number | null = null;
      try {
        priorCount = await kv.get<number>(idemStorageKey, "json");
      } catch {
        // KV read failure -> fall through to a normal (possibly re-incrementing)
        // copy rather than failing the request.
        priorCount = null;
      }
      if (typeof priorCount === "number") {
        return jsonResponse({ copies_count: priorCount, envelope });
      }
    }

    const updated = await env.DB.prepare(
      `UPDATE specimens
       SET copies_count = copies_count + 1
       WHERE slug = ?
       RETURNING copies_count`
    )
      .bind(slug)
      .first<{ copies_count: number }>();

    const copies_count = Number(updated?.copies_count ?? specimen.copies_count + 1);

    // Record the result so a retry of THIS key replays it instead of bumping the
    // counter again. Best-effort: a write failure just means the next retry
    // re-increments (the pre-idempotency behavior), never a failed response.
    if (idemStorageKey && kv) {
      try {
        await kv.put(idemStorageKey, JSON.stringify(copies_count), {
          expirationTtl: IDEMPOTENCY_TTL_SEC
        });
      } catch {
        // Ignore: the increment already happened and is returned below.
      }
    }

    return jsonResponse({ copies_count, envelope });
  } catch (error) {
    console.error("POST /api/specimens/:slug/copy failed", error);
    return serverErrorResponse();
  }
};
