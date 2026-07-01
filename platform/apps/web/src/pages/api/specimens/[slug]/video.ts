import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import {
  getSpecimenForEdit,
  getVideoKeyForSlug,
  setSpecimenVideoKey,
  type SpecimenEditData
} from "../../../../server/db";
import { getCoverVideo, putCoverVideo } from "../../../../server/r2";
import { getRequestUser, requireUser } from "../../../../server/auth";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";

export const prerender = false;

// GET /api/specimens/:slug/video -- serve the author-uploaded cover video (stored
// in R2 under the specimen's video_key) with HTTP Range support. 404 when the
// specimen has no uploaded video; the UI only points here when one exists,
// falling back to the poster thumbnail otherwise. <video> requests carry
// same-origin cookies, so the author's session resolves here -- letting an owner
// see their OWN private/unlisted draft's cover (visibility rule mirrors
// getSpecimenBySlug); other viewers still only get public ones.
//
// Range support is mandatory: Safari refuses to play (and cannot scrub) an MP4
// served without Accept-Ranges + 206 responses. A "bytes=start-end" request
// (end optional) yields a 206 with Content-Range; an out-of-range start yields a
// 416; no Range header yields the full 200 body.
export const GET: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) return errorResponse(400, "invalid_slug", "A specimen slug is required.");

    const viewer = await getRequestUser(request, env);
    const key = await getVideoKeyForSlug(env.DB, slug, viewer?.id);
    if (!key) return errorResponse(404, "video_not_found", "No cover video for that specimen.");

    // Anonymous requests can only ever resolve a PUBLIC video (viewer id is
    // undefined), so a shared CDN cache is safe. An authenticated request may
    // resolve the owner's OWN private/unlisted draft cover -- that must never
    // land in a shared cache, so mark it private (browser-only, short-lived).
    const cacheControl = viewer
      ? "private, max-age=60"
      : "public, max-age=300, s-maxage=86400";

    // Parse a "bytes=start-end" Range header (end optional). Only a single,
    // well-formed byte range is honored; anything else falls through to a full
    // 200 body, which is a valid response to a Range request.
    const requestedRange = parseByteRange(request.headers.get("Range"));

    if (requestedRange) {
      const length =
        requestedRange.end !== undefined
          ? requestedRange.end - requestedRange.start + 1
          : undefined;

      const object = await getCoverVideo(env.BLOBS, key, {
        offset: requestedRange.start,
        length
      });

      // R2 returns null for a range whose start is at/past EOF -- which is
      // indistinguishable from a missing blob. ONLY in that case do we spend a
      // head() to tell 416 (blob exists, start past end) from 404 (blob gone).
      // The common, satisfiable path issues a single R2 op (no extra head).
      if (!object) {
        const head = await env.BLOBS.head(key);
        if (!head) return errorResponse(404, "video_not_found", "The video blob is missing.");
        return errorResponse(416, "range_not_satisfiable", "Requested range not satisfiable.", {
          "Content-Range": `bytes */${head.size}`
        });
      }

      // R2 clamps the served slice to the object bounds and reports the actual
      // offset/length back on object.range -- use those for Content-Range and
      // Content-Length, and object.size (the FULL size) for the range total.
      const servedOffset = object.range && "offset" in object.range && object.range.offset !== undefined
        ? object.range.offset
        : requestedRange.start;
      const servedLength =
        object.range && "length" in object.range && object.range.length !== undefined
          ? object.range.length
          : object.size - servedOffset;
      const servedEnd = servedOffset + servedLength - 1;

      return new Response(object.body, {
        status: 206,
        headers: {
          "Content-Type": object.httpMetadata?.contentType || "video/mp4",
          "Content-Range": `bytes ${servedOffset}-${servedEnd}/${object.size}`,
          "Accept-Ranges": "bytes",
          "Content-Length": String(servedLength),
          "Cache-Control": cacheControl,
          "Vary": "Cookie",
          ETag: object.httpEtag
        }
      });
    }

    // No Range header: serve the full object.
    const object = await getCoverVideo(env.BLOBS, key);
    if (!object) return errorResponse(404, "video_not_found", "The video blob is missing.");

    return new Response(object.body, {
      status: 200,
      headers: {
        "Content-Type": object.httpMetadata?.contentType || "video/mp4",
        "Accept-Ranges": "bytes",
        "Content-Length": String(object.size),
        "Cache-Control": cacheControl,
        "Vary": "Cookie",
        ETag: object.httpEtag
      }
    });
  } catch (error) {
    console.error("GET /api/specimens/[slug]/video failed", error);
    return serverErrorResponse();
  }
};

// Parse a single "bytes=start-end" Range header. Returns { start, end? } for a
// well-formed range, or null for a missing / malformed / multi-range header (the
// caller then serves the full body, a valid answer per RFC 7233). The end is
// optional ("bytes=500-" means "from 500 to EOF").
function parseByteRange(header: string | null): { start: number; end?: number } | null {
  if (!header) return null;

  const match = /^bytes=(\d+)-(\d*)$/.exec(header.trim());
  if (!match) return null;

  const start = Number(match[1]);
  if (!Number.isFinite(start)) return null;

  if (match[2] === "") return { start };

  const end = Number(match[2]);
  if (!Number.isFinite(end) || end < start) return null;

  return { start, end };
}

// POST /api/specimens/:slug/video -- attach or replace the author-uploaded cover
// video. Author-auth-gated (session required, must be the specimen's author),
// mirroring the ownership gate the PUT metadata endpoint uses. The video arrives
// as raw bytes -- a 10 MB MP4 is far too large to base64 into JSON -- via either
// a multipart/form-data "video" field or an application/octet-stream raw body.
// putCoverVideo re-validates size + MP4 magic bytes (returns null on failure);
// on success we point video_key at the new blob. The poster in thumbnail_key is
// left untouched, so the grid/og pipeline keeps working unchanged.
export const POST: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) return errorResponse(400, "invalid_slug", "A specimen slug is required.");

    const auth = await resolveOwner(request, slug);
    if (!auth.ok) return auth.response;

    // Reject an over-cap upload by its DECLARED size before buffering the body
    // into memory -- a 100 MB body must never be materialized in the Worker just
    // to fail the 10 MB gate. A small margin covers multipart framing overhead;
    // the exact byte-length check still runs after the body is read.
    const declaredLength = Number(request.headers.get("Content-Length") ?? "");
    if (Number.isFinite(declaredLength) && declaredLength > VIDEO_MAX_BYTES + 65536) {
      return errorResponse(413, "video_too_large", "The cover video must be 10 MB or smaller.");
    }

    // Pull the uploaded bytes + declared content type from either a multipart
    // form (field "video") or an octet-stream raw body.
    const upload = await readVideoUpload(request);
    if (!upload) {
      return errorResponse(400, "invalid_upload", "A video file is required (multipart field 'video' or a raw body).");
    }

    // putCoverVideo returns null on an oversized, non-MP4, or wrong-content-type
    // payload. Distinguish the two common failures for a clearer client error.
    if (upload.bytes.byteLength > VIDEO_MAX_BYTES) {
      return errorResponse(413, "video_too_large", "The cover video must be 10 MB or smaller.");
    }
    const stored = await putCoverVideo(env.BLOBS, upload.bytes, upload.contentType);
    if (!stored) {
      return errorResponse(415, "invalid_video", "The cover video must be an MP4 (H.264) file.");
    }

    await applyVideoKey(auth.specimen, { videoKey: stored.key });

    return jsonResponse({ updated: true, slug: auth.specimen.slug, videoKey: stored.key });
  } catch (error) {
    console.error("POST /api/specimens/[slug]/video failed", error);
    return serverErrorResponse();
  }
};

// DELETE /api/specimens/:slug/video -- remove the author-uploaded cover video.
// Author-auth-gated. Clears video_key to NULL; the poster (thumbnail_key) stays,
// so the cover reverts to the image-only state. The R2 blob is content-addressed
// and left in place (lifecycle cleanup is a separate concern).
export const DELETE: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) return errorResponse(400, "invalid_slug", "A specimen slug is required.");

    const auth = await resolveOwner(request, slug);
    if (!auth.ok) return auth.response;

    await applyVideoKey(auth.specimen, { clearVideo: true });

    return jsonResponse({ updated: true, slug: auth.specimen.slug, videoKey: null });
  } catch (error) {
    console.error("DELETE /api/specimens/[slug]/video failed", error);
    return serverErrorResponse();
  }
};

// 10 MB cap, mirrored from putCoverVideo (r2.ts) so we can answer 413 before the
// magic-byte check when the body is simply too large.
const VIDEO_MAX_BYTES = 10 * 1024 * 1024;

// Read the uploaded video bytes + content type from the request. Accepts a
// multipart/form-data body with a "video" file field, or a raw octet-stream (or
// video/*) body. Returns null when no usable bytes are present.
async function readVideoUpload(
  request: Request
): Promise<{ bytes: Uint8Array; contentType: string } | null> {
  const type = request.headers.get("Content-Type") ?? "";

  if (type.includes("multipart/form-data")) {
    let form: FormData;
    try {
      form = await request.formData();
    } catch {
      return null;
    }
    const file = form.get("video");
    if (!(file instanceof File)) return null;
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (bytes.byteLength === 0) return null;
    // Prefer the file's own type; the magic-byte check in putCoverVideo is the
    // real gate, so a missing/blank type is coerced to the MP4 whitelist value.
    return { bytes, contentType: file.type || "video/mp4" };
  }

  // Raw body (application/octet-stream or video/mp4). The declared content type
  // is passed through to putCoverVideo, which enforces the MP4 whitelist.
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.byteLength === 0) return null;
  const contentType = type.startsWith("video/") ? type : "video/mp4";
  return { bytes, contentType };
}

// Point (or clear) the specimen's video_key WITHOUT disturbing any other
// metadata. A dedicated single-column UPDATE (setSpecimenVideoKey) is used
// instead of updateSpecimenMetadata so a cover-video attach/remove never re-runs
// the FTS re-sync -- which would otherwise risk wiping the search dat_text when
// the TDN blob failed to reload. `patch` is either { videoKey } (attach/replace)
// or { clearVideo: true } (remove).
async function applyVideoKey(
  specimen: SpecimenEditData,
  patch: { videoKey: string } | { clearVideo: true }
): Promise<void> {
  const videoKey = "videoKey" in patch ? patch.videoKey : null;
  await setSpecimenVideoKey(env.DB, specimen.id, videoKey);
}

type OwnerResult =
  | { ok: true; specimen: SpecimenEditData }
  | { ok: false; response: Response };

// Shared gate for POST/DELETE: require a session, load the specimen, and confirm
// the signed-in user is its author. Mirrors resolveOwner in ../[slug].ts (401 if
// unauthenticated, 404 if no such specimen, 403 if not the author). Ownership is
// enforced by author_id (not the handle) -- the authoritative key.
async function resolveOwner(request: Request, slug: string): Promise<OwnerResult> {
  let user;
  try {
    user = await requireUser(request, env);
  } catch {
    return {
      ok: false,
      response: errorResponse(401, "authentication_required", "A signed-in user is required.")
    };
  }

  const specimen = await getSpecimenForEdit(env.DB, slug);
  if (!specimen) {
    return {
      ok: false,
      response: errorResponse(404, "specimen_not_found", "No specimen exists for that slug.")
    };
  }

  if (specimen.authorId !== user.id) {
    return {
      ok: false,
      response: errorResponse(403, "forbidden", "You can only modify your own specimens.")
    };
  }

  return { ok: true, specimen };
}
