import {
  EMBODY_TDXN_MARKER,
  EMBODY_TDXN_VERSION,
  canonicalTdxnString,
  type EmbodyTdxnEnvelope
} from "@embody/contracts";

const encoder = new TextEncoder();

export function canonicalTdxnBytes(tdxn: Record<string, unknown>): Uint8Array {
  return encoder.encode(stableJsonStringify(tdxn));
}

export async function canonicalTdxnSha256(tdxn: Record<string, unknown>): Promise<string> {
  const bytes = canonicalTdxnBytes(tdxn);
  const buffer = new Uint8Array(bytes).buffer;
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export async function buildEmbodyEnvelope(
  tdxn: Record<string, unknown>,
  options: { slug?: string; version?: number } = {}
): Promise<EmbodyTdxnEnvelope> {
  const envelope: EmbodyTdxnEnvelope = {
    [EMBODY_TDXN_MARKER]: EMBODY_TDXN_VERSION,
    source: "embody.tools",
    // Fresh per copy so each Copy is a distinct clipboard payload -- this is what
    // lets the TD-side watcher re-prompt on a re-copy. Not part of the sha256.
    copy_id: crypto.randomUUID(),
    sha256: await canonicalTdxnSha256(tdxn),
    tdn: tdxn
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
  return canonicalTdxnString(value);
}
