import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getCurrentTdnBlobForSlug } from "../../../../server/db";
import { errorResponse, serverErrorResponse } from "../../../../server/http";
import { getTdn } from "../../../../server/r2";

export const prerender = false;

export const GET: APIRoute = async ({ params }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    const blob = await getCurrentTdnBlobForSlug(env.DB, slug);
    if (!blob) {
      return errorResponse(404, "tdn_not_found", "No TDN blob exists for that slug.");
    }

    const tdn = await getTdn(env.BLOBS, blob.key);
    if (!tdn) {
      return errorResponse(404, "tdn_not_found", "The TDN blob is missing from R2.");
    }

    return new Response(tdn, {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "public, max-age=300, s-maxage=3600",
        "X-Embody-Scan-Verdict": blob.capability.verdict
      }
    });
  } catch {
    return serverErrorResponse();
  }
};
