import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getThumbnailKeyForSlug } from "../../../../server/db";
import { getThumbnail } from "../../../../server/r2";
import { getRequestUser } from "../../../../server/auth";
import { errorResponse, serverErrorResponse } from "../../../../server/http";

export const prerender = false;

// GET /api/specimens/:slug/thumbnail -- serve the specimen's cover image (R2,
// under the row's thumbnail_key: an author upload, or the repo cover the
// first-party sync points at). 404 when the row has no thumbnail; the UI only
// points here when one exists, falling back to the procedural placeholder.
// <img> requests carry same-origin cookies, so the author's session resolves here
// -- letting an owner see their OWN private/unlisted draft's cover (visibility
// rule mirrors getSpecimenBySlug); other viewers still only get public ones.
export const GET: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) return errorResponse(400, "invalid_slug", "A specimen slug is required.");

    const viewer = await getRequestUser(request, env);
    const key = await getThumbnailKeyForSlug(env.DB, slug, viewer?.id);
    if (!key) return errorResponse(404, "thumbnail_not_found", "No thumbnail for that specimen.");

    const object = await getThumbnail(env.BLOBS, key);
    if (!object) return errorResponse(404, "thumbnail_not_found", "The thumbnail blob is missing.");

    // Anonymous requests can only ever resolve a PUBLIC thumbnail (viewer id is
    // undefined), so a shared CDN cache is safe. An authenticated request may
    // resolve the owner's OWN private/unlisted draft cover -- that must never
    // land in a shared cache, so mark it private (browser-only, short-lived).
    const cacheControl = viewer
      ? "private, max-age=60"
      : "public, max-age=300, s-maxage=86400";

    return new Response(object.body, {
      status: 200,
      headers: {
        "Content-Type": object.httpMetadata?.contentType || "image/png",
        "Cache-Control": cacheControl,
        ETag: object.httpEtag
      }
    });
  } catch (error) {
    console.error("GET /api/specimens/[slug]/thumbnail failed", error);
    return serverErrorResponse();
  }
};
