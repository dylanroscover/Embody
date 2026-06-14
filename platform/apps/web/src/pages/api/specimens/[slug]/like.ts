import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { requireUser } from "../../../../server/auth";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";
import { getSpecimenIdBySlug, toggleLike } from "../../../../server/engagement";

export const prerender = false;

// POST /api/specimens/:slug/like
// Toggles the signed-in user's like on a specimen. Anonymous callers get 401
// (the page links anon visitors to /signin instead of calling this). Resolves
// the specimen id from the slug, flips the like row, keeps the denormalized
// likes_count in step, and returns { liked, likes_count }.
export const POST: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    let user;
    try {
      user = await requireUser(request, env);
    } catch {
      return errorResponse(401, "authentication_required", "A signed-in user is required to like.");
    }

    const specimenId = await getSpecimenIdBySlug(env.DB, slug);
    if (!specimenId) {
      return errorResponse(404, "specimen_not_found", "No specimen exists for that slug.");
    }

    const result = await toggleLike(env.DB, specimenId, user.id);
    return jsonResponse(result);
  } catch (error) {
    console.error("POST /api/specimens/:slug/like failed", error);
    return serverErrorResponse();
  }
};
