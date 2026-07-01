import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { assertSameOrigin, clientIp, isTier, isVisibility, requireAdmin } from "../../../../server/admin";
import { deleteSpecimenById, logEvent, updateSpecimenModeration } from "../../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";

export const prerender = false;

// Admin-only: change a specimen's visibility and/or tier. `id` is the specimen
// primary key (as listed by listSpecimensForAdmin), not the slug.
export const POST: APIRoute = async ({ params, request }) => {
  try {
    let admin;
    try {
      admin = await requireAdmin(request, env);
    } catch (res) {
      return res as Response;
    }
    const csrf = assertSameOrigin(request);
    if (csrf) return csrf;

    const id = params.id;
    if (!id) return errorResponse(400, "invalid_id", "A specimen id is required.");

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_body", "A JSON body is required.");
    }
    const raw = (body ?? {}) as { visibility?: unknown; tier?: unknown };

    const patch: { visibility?: string; tier?: string } = {};
    if (raw.visibility !== undefined) {
      if (!isVisibility(raw.visibility)) {
        return errorResponse(
          400,
          "invalid_visibility",
          "visibility must be one of: public, unlisted, private."
        );
      }
      patch.visibility = raw.visibility;
    }
    if (raw.tier !== undefined) {
      if (!isTier(raw.tier)) {
        return errorResponse(400, "invalid_tier", "tier must be one of: community, verified, featured.");
      }
      patch.tier = raw.tier;
    }
    if (patch.visibility === undefined && patch.tier === undefined) {
      return errorResponse(400, "invalid_request", "Provide visibility and/or tier.");
    }

    const ok = await updateSpecimenModeration(env.DB, id, patch);
    if (!ok) return errorResponse(404, "specimen_not_found", "No specimen exists for that id.");

    await logEvent(env.DB, {
      actorId: admin.id,
      actorHandle: admin.handle,
      action: "specimen.moderate",
      targetType: "specimen",
      targetId: id,
      metadata: patch,
      ip: clientIp(request)
    });
    return jsonResponse({ updated: true, id, ...patch });
  } catch (error) {
    console.error("POST /api/admin/specimens/[id] failed", error);
    return serverErrorResponse();
  }
};

// Admin-only: hard-delete a specimen and all its dependent rows (reuses the
// owner-delete helper, which cascades scans/versions/tags/likes/comments/
// reports/reactions).
export const DELETE: APIRoute = async ({ params, request }) => {
  try {
    let admin;
    try {
      admin = await requireAdmin(request, env);
    } catch (res) {
      return res as Response;
    }
    const csrf = assertSameOrigin(request);
    if (csrf) return csrf;

    const id = params.id;
    if (!id) return errorResponse(400, "invalid_id", "A specimen id is required.");

    await deleteSpecimenById(env.DB, id);
    await logEvent(env.DB, {
      actorId: admin.id,
      actorHandle: admin.handle,
      action: "specimen.delete",
      targetType: "specimen",
      targetId: id,
      ip: clientIp(request)
    });
    return jsonResponse({ deleted: true, id });
  } catch (error) {
    console.error("DELETE /api/admin/specimens/[id] failed", error);
    return serverErrorResponse();
  }
};
