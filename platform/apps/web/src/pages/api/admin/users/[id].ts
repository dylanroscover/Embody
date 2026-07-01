import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { assertSameOrigin, clientIp, isTrustLevel, requireAdmin } from "../../../../server/admin";
import { deleteUserAccount, logEvent, setUserBanned, setUserTrustLevel } from "../../../../server/db";
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

    await logEvent(env.DB, {
      actorId: admin.id,
      actorHandle: admin.handle,
      action: "user.trust_level",
      targetType: "user",
      targetId: id,
      metadata: { trustLevel },
      ip: clientIp(request)
    });
    return jsonResponse({ updated: true, id, trustLevel });
  } catch (error) {
    console.error("POST /api/admin/users/[id] failed", error);
    return serverErrorResponse();
  }
};

// Admin-only: ban or unban an account. Banning makes them resolve as logged-out
// everywhere (cannot sign in / submit) and drops their specimens from every
// public listing -- reversible by unbanning. An admin cannot ban themselves.
export const PATCH: APIRoute = async ({ params, request }) => {
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
      return errorResponse(400, "invalid_body", "A JSON body with `banned` is required.");
    }
    const raw = (body ?? {}) as { banned?: unknown; reason?: unknown };
    if (typeof raw.banned !== "boolean") {
      return errorResponse(400, "invalid_request", "`banned` must be a boolean.");
    }
    if (raw.banned && id === admin.id) {
      return errorResponse(409, "self_ban_blocked", "You cannot ban your own account.");
    }
    const reason =
      typeof raw.reason === "string" && raw.reason.trim() ? raw.reason.trim().slice(0, 500) : null;

    const ok = await setUserBanned(env.DB, id, raw.banned, reason);
    if (!ok) return errorResponse(404, "user_not_found", "No user exists for that id.");

    await logEvent(env.DB, {
      actorId: admin.id,
      actorHandle: admin.handle,
      action: raw.banned ? "user.ban" : "user.unban",
      targetType: "user",
      targetId: id,
      metadata: reason ? { reason } : null,
      ip: clientIp(request)
    });
    return jsonResponse({ updated: true, id, banned: raw.banned });
  } catch (error) {
    console.error("PATCH /api/admin/users/[id] failed", error);
    return serverErrorResponse();
  }
};

// Admin-only: HARD-DELETE a user account -- their profile, every specimen they
// authored (and each specimen's dependents), all of their engagement on other
// specimens, and their Better Auth identity. Irreversible; the panel confirms
// first. An admin cannot delete their own account (mirrors the self-demote guard).
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
    if (!id) return errorResponse(400, "invalid_id", "A user id is required.");

    if (id === admin.id) {
      return errorResponse(409, "self_delete_blocked", "You cannot delete your own account.");
    }

    const ok = await deleteUserAccount(env.DB, id);
    if (!ok) return errorResponse(404, "user_not_found", "No user exists for that id.");

    await logEvent(env.DB, {
      actorId: admin.id,
      actorHandle: admin.handle,
      action: "user.delete",
      targetType: "user",
      targetId: id,
      ip: clientIp(request)
    });
    return jsonResponse({ deleted: true, id });
  } catch (error) {
    console.error("DELETE /api/admin/users/[id] failed", error);
    return serverErrorResponse();
  }
};
