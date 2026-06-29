import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getRequestUser } from "../../../server/auth";
import { setProfileAvatarUrl } from "../../../server/db";
import { putAvatar } from "../../../server/r2";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";
import { checkRateLimit } from "../../../server/rateLimit";

export const prerender = false;

// Per-user cap on avatar uploads. Each POST base64-decodes the payload and writes
// to R2 + D1; without a bound it is a cheap per-account CPU/storage abuse path.
const AVATAR_RATE_LIMIT = { limit: 20, windowSec: 300 };

// POST /api/account/avatar -- the signed-in user uploads a new avatar. Body is
// { avatar: <data URL> }; the client downscales/squares the image to a small
// PNG/JPEG/WebP before sending. The blob is stored content-addressed in R2
// (avatars/<sha256>) and users_profile.avatar_url is set to the serve URL, which
// overrides the GitHub-seeded avatar. Returns { avatar_url }.
export const POST: APIRoute = async ({ request }) => {
  try {
    const user = await getRequestUser(request, env);
    if (!user) return errorResponse(401, "unauthorized", "Sign in to set an avatar.");

    const rate = await checkRateLimit(env.KV, `avatar:${user.id}`, AVATAR_RATE_LIMIT);
    if (!rate.ok) {
      return errorResponse(
        429,
        "rate_limited",
        "Too many avatar updates. Please slow down and try again shortly.",
        rate.retryAfter ? { "Retry-After": String(rate.retryAfter) } : undefined
      );
    }

    let body: { avatar?: unknown };
    try {
      body = (await request.json()) as { avatar?: unknown };
    } catch {
      return errorResponse(400, "invalid_body", "Expected a JSON body with an avatar data URL.");
    }

    const dataUrl = typeof body.avatar === "string" ? body.avatar : undefined;
    // putAvatar returns null for a non-image, oversized, or unparseable payload.
    const stored = await putAvatar(env.BLOBS, dataUrl);
    if (!stored) {
      return errorResponse(
        400,
        "invalid_avatar",
        "Avatar must be a PNG, JPEG, or WebP image under 0.5 MB."
      );
    }

    const avatarUrl = `/api/avatars/${stored.sha256}`;
    await setProfileAvatarUrl(env.DB, user.id, avatarUrl);
    return jsonResponse({ avatar_url: avatarUrl });
  } catch (error) {
    console.error("POST /api/account/avatar failed", error);
    return serverErrorResponse();
  }
};

// DELETE /api/account/avatar -- remove the custom avatar. Clears avatar_url; on
// the next authed request getSessionUser re-seeds the GitHub avatar if there is
// one (so "remove" reverts to the GitHub default, else the letter chip).
export const DELETE: APIRoute = async ({ request }) => {
  try {
    const user = await getRequestUser(request, env);
    if (!user) return errorResponse(401, "unauthorized", "Sign in to change your avatar.");

    await setProfileAvatarUrl(env.DB, user.id, null);
    return jsonResponse({ avatar_url: null });
  } catch (error) {
    console.error("DELETE /api/account/avatar failed", error);
    return serverErrorResponse();
  }
};
