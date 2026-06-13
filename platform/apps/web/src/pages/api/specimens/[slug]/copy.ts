import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getSpecimenBySlug } from "../../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";
import { getParsedTdnForSlug } from "../../../../server/tdn";
import { buildEmbodyEnvelope } from "../../../../lib/tdnEnvelope";

export const prerender = false;

// POST /api/specimens/:slug/copy
// Builds the `_embody_tdn` clipboard envelope for a specimen from its real R2
// TDN blob, increments the copies_count counter, and returns both. The envelope
// source is "embody.tools" (community provenance) so the Embody TD side imports
// it default-inert (sandboxed paste). Returns { copies_count, envelope }.
export const POST: APIRoute = async ({ params }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
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

    const updated = await env.DB.prepare(
      `UPDATE specimens
       SET copies_count = copies_count + 1
       WHERE slug = ?
       RETURNING copies_count`
    )
      .bind(slug)
      .first<{ copies_count: number }>();

    const copies_count = Number(updated?.copies_count ?? specimen.copies_count + 1);

    return jsonResponse({ copies_count, envelope });
  } catch (error) {
    console.error("POST /api/specimens/:slug/copy failed", error);
    return serverErrorResponse();
  }
};
