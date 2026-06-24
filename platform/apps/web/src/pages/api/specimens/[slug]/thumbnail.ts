import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getThumbnailKeyForSlug } from "../../../../server/db";
import { getThumbnail } from "../../../../server/r2";
import { errorResponse, serverErrorResponse } from "../../../../server/http";

export const prerender = false;

// GET /api/specimens/:slug/thumbnail — serve the author-uploaded thumbnail image
// (stored in R2 under the specimen's thumbnail_key) for the result cover. 404
// when the specimen has no uploaded thumbnail; the UI only points here when one
// exists, falling back to the baked render / procedural placeholder otherwise.
export const GET: APIRoute = async ({ params }) => {
  try {
    const slug = params.slug;
    if (!slug) return errorResponse(400, "invalid_slug", "A specimen slug is required.");

    const key = await getThumbnailKeyForSlug(env.DB, slug);
    if (!key) return errorResponse(404, "thumbnail_not_found", "No thumbnail for that specimen.");

    const object = await getThumbnail(env.BLOBS, key);
    if (!object) return errorResponse(404, "thumbnail_not_found", "The thumbnail blob is missing.");

    return new Response(object.body, {
      status: 200,
      headers: {
        "Content-Type": object.httpMetadata?.contentType || "image/png",
        "Cache-Control": "public, max-age=300, s-maxage=86400",
        ETag: object.httpEtag
      }
    });
  } catch (error) {
    console.error("GET /api/specimens/[slug]/thumbnail failed", error);
    return serverErrorResponse();
  }
};
