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

export async function putThumbnail(
  blobs: R2Bucket,
  thumbnail: string | undefined
): Promise<BlobWrite | null> {
  if (!thumbnail) return null;

  const parsed = parseDataUrl(thumbnail);
  if (!parsed) return null;

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
