export interface BlobWrite {
  key: string;
  sha256: string;
}

const encoder = new TextEncoder();

export async function putTdn(blobs: R2Bucket, jsonString: string): Promise<BlobWrite> {
  const bytes = encoder.encode(jsonString);
  const sha256 = await sha256Hex(bytes);
  const key = sha256;

  await blobs.put(key, bytes, {
    httpMetadata: {
      contentType: "application/json"
    },
    customMetadata: {
      sha256
    }
  });

  return { key, sha256 };
}

export async function getTdn(blobs: R2Bucket, key: string): Promise<string | null> {
  if (!key) return null;

  const object = await blobs.get(key);
  if (!object) return null;

  return object.text();
}

// Fetch a stored thumbnail blob for streaming (its body + content type). Null
// when the key is empty or the object is missing.
export async function getThumbnail(blobs: R2Bucket, key: string): Promise<R2ObjectBody | null> {
  if (!key) return null;
  return blobs.get(key);
}

// Thumbnails are resized to a 640x360 WebP on the client before upload, so they
// land around 15-50 KB. Validate content type + a generous byte cap here as
// defense in depth: reject a non-image or an oversized payload so a caller that
// bypasses the client resizer can't store a multi-MB original for a card slot.
const THUMBNAIL_MAX_BYTES = 500 * 1024; // 0.5 MB -- resized WebP is far smaller
const THUMBNAIL_CONTENT_TYPES = new Set(["image/webp", "image/png", "image/jpeg"]);

export async function putThumbnail(
  blobs: R2Bucket,
  thumbnail: string | undefined
): Promise<BlobWrite | null> {
  if (!thumbnail) return null;

  const parsed = parseDataUrl(thumbnail);
  if (!parsed) return null;
  if (!THUMBNAIL_CONTENT_TYPES.has(parsed.contentType)) return null;
  if (parsed.bytes.byteLength === 0 || parsed.bytes.byteLength > THUMBNAIL_MAX_BYTES) return null;

  const sha256 = await sha256Hex(parsed.bytes);
  const key = `thumbnails/${sha256}`;

  await blobs.put(key, parsed.bytes, {
    httpMetadata: {
      contentType: parsed.contentType
    },
    customMetadata: {
      sha256
    }
  });

  return { key, sha256 };
}

// Avatars: content-addressed under avatars/<sha256>, uploaded as a data URL just
// like thumbnails. Content addressing means a re-upload gets a NEW key, so the
// serve URL changes and no cache-busting is needed. Returns null on a non-image,
// oversized, or unparseable payload so the route can answer 400 cleanly.
const AVATAR_MAX_BYTES = 512 * 1024; // 0.5 MB -- avatars are downscaled client-side
const AVATAR_CONTENT_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

export async function putAvatar(
  blobs: R2Bucket,
  dataUrl: string | undefined
): Promise<BlobWrite | null> {
  if (!dataUrl) return null;

  const parsed = parseDataUrl(dataUrl);
  if (!parsed) return null;
  if (!AVATAR_CONTENT_TYPES.has(parsed.contentType)) return null;
  if (parsed.bytes.byteLength === 0 || parsed.bytes.byteLength > AVATAR_MAX_BYTES) return null;

  const sha256 = await sha256Hex(parsed.bytes);
  const key = `avatars/${sha256}`;

  await blobs.put(key, parsed.bytes, {
    httpMetadata: { contentType: parsed.contentType },
    customMetadata: { sha256 }
  });

  return { key, sha256 };
}

// Fetch a stored avatar blob for streaming. `sha256` is the bare hash from the
// serve URL; null when empty or missing.
export async function getAvatar(blobs: R2Bucket, sha256: string): Promise<R2ObjectBody | null> {
  if (!sha256) return null;
  return blobs.get(`avatars/${sha256}`);
}

// Cover videos: content-addressed under videos/<sha256>, uploaded as raw bytes
// (a 10 MB video is far too large to base64 through a JSON data URL, so the
// caller passes a Uint8Array from a multipart body). MP4 / H.264 only. Validate
// by magic bytes -- a valid MP4 opens with an "ftyp" box whose four-char code
// sits at bytes 4-7 -- not by the client-supplied content type alone. Returns
// null on an empty, oversized, non-MP4, or wrong-content-type payload so the
// route can answer 400 cleanly.
const VIDEO_MAX_BYTES = 10 * 1024 * 1024; // 10 MB
const VIDEO_CONTENT_TYPE = "video/mp4";

export async function putCoverVideo(
  blobs: R2Bucket,
  bytes: Uint8Array,
  contentType: string
): Promise<BlobWrite | null> {
  // Reject empty, sub-header (fewer than 8 bytes -- too short to even hold the
  // ftyp box we read below), or oversized payloads before hashing.
  if (bytes.byteLength < 8 || bytes.byteLength > VIDEO_MAX_BYTES) return null;

  // Magic bytes: the MP4 ftyp box code (ASCII "ftyp") lives at offset 4-7.
  if (
    bytes[4] !== 0x66 || // f
    bytes[5] !== 0x74 || // t
    bytes[6] !== 0x79 || // y
    bytes[7] !== 0x70    // p
  ) {
    return null;
  }

  if (contentType !== VIDEO_CONTENT_TYPE) return null;

  const sha256 = await sha256Hex(bytes);
  const key = `videos/${sha256}`;

  await blobs.put(key, bytes, {
    httpMetadata: { contentType: VIDEO_CONTENT_TYPE },
    customMetadata: { sha256 }
  });

  return { key, sha256 };
}

// Fetch a stored cover video blob for streaming. `range` is passed straight
// through to R2 so the serve endpoint can request a byte range (an { offset,
// length } object or a Headers with a Range) for HTTP 206 responses. Null when
// the key is empty or the object is missing.
export async function getCoverVideo(
  blobs: R2Bucket,
  key: string,
  range?: R2Range | Headers
): Promise<R2ObjectBody | null> {
  if (!key) return null;
  return range ? blobs.get(key, { range }) : blobs.get(key);
}

export function byteLength(value: string): number {
  return encoder.encode(value).byteLength;
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const buffer = new Uint8Array(bytes).buffer;
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function parseDataUrl(dataUrl: string): { bytes: Uint8Array; contentType: string } | null {
  const match = /^data:([^;,]+)?(;base64)?,(.*)$/s.exec(dataUrl);
  if (!match) return null;

  const contentType = match[1] || "application/octet-stream";
  const isBase64 = match[2] === ";base64";
  const payload = match[3] || "";

  if (isBase64) {
    try {
      const binary = atob(payload);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
      }
      return { bytes, contentType };
    } catch {
      return null;
    }
  }

  try {
    return { bytes: encoder.encode(decodeURIComponent(payload)), contentType };
  } catch {
    return null;
  }
}
