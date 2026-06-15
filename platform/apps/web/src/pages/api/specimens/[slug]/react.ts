import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { isReactionEmoji } from "../../../../lib/reactions";
import { requireUser } from "../../../../server/auth";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";
import { getSpecimenIdBySlug, toggleReaction } from "../../../../server/engagement";

export const prerender = false;

// POST /api/specimens/:slug/react   body: { emoji }
// Toggles the signed-in user's reaction with `emoji` on a specimen. Anonymous
// callers get 401 (the UI bounces them to /signin instead of calling this). The
// emoji is validated against the reaction allow-list. Resolves the specimen id
// from the slug, flips the reaction row, recomputes the denormalized tallies, and
// returns { emoji, reacted, reactions, mine, total }.
export const POST: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_body", "A JSON body with an emoji is required.");
    }

    const emoji = (body as { emoji?: unknown } | null)?.emoji;
    if (!isReactionEmoji(emoji)) {
      return errorResponse(400, "invalid_emoji", "That emoji is not an allowed reaction.");
    }

    let user;
    try {
      user = await requireUser(request, env);
    } catch {
      return errorResponse(401, "authentication_required", "A signed-in user is required to react.");
    }

    const specimenId = await getSpecimenIdBySlug(env.DB, slug);
    if (!specimenId) {
      return errorResponse(404, "specimen_not_found", "No specimen exists for that slug.");
    }

    const result = await toggleReaction(env.DB, specimenId, user.id, emoji);
    return jsonResponse(result);
  } catch (error) {
    console.error("POST /api/specimens/:slug/react failed", error);
    return serverErrorResponse();
  }
};
