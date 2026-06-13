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
