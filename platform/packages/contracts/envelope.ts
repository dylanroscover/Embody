// FROZEN CONTRACT C1 - the `_embody_tdn` clipboard wire format.
// Shared by: Embody Copy/Paste (Python mirror in dev/embody/Embody/TDNExt.py),
// the web "Copy TDN" button, and the submit-form "Paste from clipboard" button.
// Do NOT change without a contract bump (notify all dependents). ASCII only.

export const EMBODY_TDN_MARKER = "_embody_tdn" as const;
export const EMBODY_TDN_VERSION = 1 as const;

export type EnvelopeSource = "embody" | "embody.tools";

export interface EmbodyTdnEnvelope {
  /** Detection marker; always 1 for this version. */
  _embody_tdn: 1;
  /**
   * Where the payload came from.
   * "embody"        = a user's own network round-tripping via Copy tdn (TRUSTED -> direct import).
   * "embody.tools"  = community content (UNTRUSTED -> default-inert safe-import on the TD side).
   */
  source: EnvelopeSource;
  /** Platform slug, present when copied from a specimen page. */
  slug?: string;
  /** Specimen version number, when applicable. */
  version?: number;
  /** sha256 of the canonical-serialized `tdn` payload (integrity + content addressing). */
  sha256: string;
  /**
   * Per-copy nonce (random per Copy action). NOT part of the sha256 / integrity
   * check and ignored by every validator -- it only makes each copy a distinct
   * clipboard payload so the Embody TD-side clipboard watcher re-prompts when a
   * user re-copies the same network (otherwise an identical envelope reads as
   * "same content still on the clipboard" and is debounced).
   */
  copy_id?: string;
  /** The full TDN network dict. Schema: docs/tdn/specification.md (contract C7). */
  tdn: Record<string, unknown>;
}

/** Type guard: is an arbitrary parsed-JSON value a valid envelope? */
export function isEmbodyTdnEnvelope(v: unknown): v is EmbodyTdnEnvelope {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return (
    o[EMBODY_TDN_MARKER] === EMBODY_TDN_VERSION &&
    (o.source === "embody" || o.source === "embody.tools") &&
    typeof o.sha256 === "string" &&
    typeof o.tdn === "object" &&
    o.tdn !== null
  );
}
