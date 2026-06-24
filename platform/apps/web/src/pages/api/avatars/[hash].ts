import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getAvatar } from "../../../server/r2";
import { errorResponse, serverErrorResponse } from "../../../server/http";

export const prerender = false;

// GET /api/avatars/:hash -- serve an uploaded avatar blob from R2. The hash is the
// sha256 of the image bytes (content-addressed), so the response is immutable and
// safe to cache for a year: a new upload yields a new hash + URL. 404 when the
// blob is missing. Avatars are public, so no auth gate.
const HASH_RE = /^[a-f0-9]{64}$/;

export const GET: APIRoute = async ({ params }) => {
  try {
    const hash = params.hash;
    if (!hash || !HASH_RE.test(hash)) {
      return errorResponse(400, "invalid_avatar", "Malformed avatar id.");
    }

    const object = await getAvatar(env.BLOBS, hash);
    if (!object) return errorResponse(404, "avatar_not_found", "No avatar for that id.");

    return new Response(object.body, {
      status: 200,
      headers: {
        "Content-Type": object.httpMetadata?.contentType || "image/png",
        "Cache-Control": "public, max-age=31536000, immutable",
        ETag: object.httpEtag
      }
    });
  } catch (error) {
    console.error("GET /api/avatars/[hash] failed", error);
    return serverErrorResponse();
  }
};
