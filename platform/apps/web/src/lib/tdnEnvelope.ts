import {
  EMBODY_TDN_MARKER,
  EMBODY_TDN_VERSION,
  canonicalTdnString,
  type EmbodyTdnEnvelope
} from "@embody/contracts";

const encoder = new TextEncoder();

export function canonicalTdnBytes(tdn: Record<string, unknown>): Uint8Array {
  return encoder.encode(stableJsonStringify(tdn));
}

export async function canonicalTdnSha256(tdn: Record<string, unknown>): Promise<string> {
  const bytes = canonicalTdnBytes(tdn);
  const buffer = new Uint8Array(bytes).buffer;
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export async function buildEmbodyEnvelope(
  tdn: Record<string, unknown>,
  options: { slug?: string; version?: number } = {}
): Promise<EmbodyTdnEnvelope> {
  const envelope: EmbodyTdnEnvelope = {
    [EMBODY_TDN_MARKER]: EMBODY_TDN_VERSION,
    source: "embody.tools",
    // Fresh per copy so each Copy is a distinct clipboard payload -- this is what
    // lets the TD-side watcher re-prompt on a re-copy. Not part of the sha256.
    copy_id: crypto.randomUUID(),
    sha256: await canonicalTdnSha256(tdn),
    tdn
  };

  if (options.slug) {
    envelope.slug = options.slug;
  }

  if (typeof options.version === "number" && Number.isFinite(options.version)) {
    envelope.version = options.version;
  }

  return envelope;
}

function stableJsonStringify(value: unknown): string {
  return canonicalTdnString(value);
}
