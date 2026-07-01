import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getRequestUser } from "../../../server/auth";
import { updateUserProfile } from "../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";

export const prerender = false;

// POST /api/account/profile -- the signed-in user updates their identity:
// { handle, displayName }. The handle is validated (3-30 chars, lowercase
// a-z0-9 + single hyphens, not reserved) and enforced unique server-side; the
// display name is optional free-form text. Returns { handle } (the saved,
// normalized handle -- the caller redirects to /u/<handle>).
export const POST: APIRoute = async ({ request }) => {
  try {
    const user = await getRequestUser(request, env);
    if (!user) return errorResponse(401, "unauthorized", "Sign in to edit your profile.");

    let body: { handle?: unknown; displayName?: unknown };
    try {
      body = (await request.json()) as { handle?: unknown; displayName?: unknown };
    } catch {
      return errorResponse(400, "invalid_body", "Expected a JSON body.");
    }

    const handle = typeof body.handle === "string" ? body.handle : "";
    const displayName = typeof body.displayName === "string" ? body.displayName : "";

    const result = await updateUserProfile(env.DB, {
      userId: user.id,
      handle,
      displayName: displayName.trim() || null
    });
    if (!result.ok) return errorResponse(400, "invalid_profile", result.detail);

    return jsonResponse({ updated: true, handle: result.handle });
  } catch {
    return serverErrorResponse();
  }
};
