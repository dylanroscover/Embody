import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { assertSameOrigin, isTrustLevel, requireAdmin } from "../../../../server/admin";
import { setUserTrustLevel } from "../../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";

export const prerender = false;

// Admin-only: change a user's trust_level (anon | verified | curator | admin).
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
    if (!id) return errorResponse(400, "invalid_id", "A user id is required.");

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_body", "A JSON body with a trustLevel is required.");
    }
    const trustLevel = (body as { trustLevel?: unknown } | null)?.trustLevel;
    if (!isTrustLevel(trustLevel)) {
      return errorResponse(
        400,
        "invalid_trust_level",
        "trustLevel must be one of: anon, verified, curator, admin."
      );
    }

    // Self-demotion lockout: an admin cannot strip their OWN admin trust. The
    // allowlist owner could re-enter, but a DB-promoted admin could not -- so
    // block it for everyone to avoid an accidental lockout.
    if (id === admin.id && trustLevel !== "admin") {
      return errorResponse(409, "self_demote_blocked", "You cannot remove your own admin access.");
    }

    const ok = await setUserTrustLevel(env.DB, id, trustLevel);
    if (!ok) return errorResponse(404, "user_not_found", "No user exists for that id.");

    console.log("ADMIN action", {
      actor: admin.id,
      action: "user.trust_level",
      target: id,
      value: trustLevel
    });
    return jsonResponse({ updated: true, id, trustLevel });
  } catch (error) {
    console.error("POST /api/admin/users/[id] failed", error);
    return serverErrorResponse();
  }
};
