// FROZEN CONTRACT C1 - the `_embody_tdn` clipboard wire format.
// Shared by: Embody Copy/Paste (Python mirror in dev/embody/Embody/TDXNExt.py),
// the web "Copy TDXN" button, and the submit-form "Paste from clipboard" button.
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
  /** The full TDXN network dict. Schema: docs/tdn/specification.md (contract C7). */
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

/**
 * Canonical serialization of a tdn payload (the bytes the envelope's sha256
 * covers). Rules, identical in dev/embody/Embody/TDXNExt.py
 * canonical_tdn_bytes: keys in Unicode code-point order, `,`/`:` separators,
 * non-ASCII emitted raw, numbers per JavaScript Number::toString (integral
 * values as integers below 1e21, fixed notation for 1e-7 <= |x| < 1e21,
 * exponent otherwise), NaN/Infinity refused. A parsed JSON payload cannot
 * tell 1.0 from 1, so Python's repr rules could never agree with this side;
 * both now follow the JavaScript rules, pinned by fixtures/canonical_cases.json.
 */
export function canonicalTdnString(value: unknown): string {
  const serialized = serializeCanonical(value, false);
  if (serialized === undefined) {
    throw new TypeError("TDXN payload must be JSON serializable.");
  }
  return serialized;
}

function serializeCanonical(value: unknown, inArray: boolean): string | undefined {
  if (value === null) return "null";
  switch (typeof value) {
    case "string":
      return JSON.stringify(value);
    case "number":
      if (!Number.isFinite(value)) {
        throw new TypeError("TDXN payload cannot contain non-finite numbers.");
      }
      return JSON.stringify(value); // Number::toString; -0 prints as 0
    case "boolean":
      return value ? "true" : "false";
    case "object":
      if (Array.isArray(value)) {
        return `[${value.map((item) => serializeCanonical(item, true) ?? "null").join(",")}]`;
      }
      return serializeCanonicalObject(value as Record<string, unknown>);
    case "undefined":
    case "function":
    case "symbol":
      return inArray ? "null" : undefined;
    case "bigint":
      throw new TypeError("TDXN payload cannot contain bigint values.");
    default:
      return undefined;
  }
}

function compareCodePoints(a: string, b: string): number {
  const ca = Array.from(a, (ch) => ch.codePointAt(0) ?? 0);
  const cb = Array.from(b, (ch) => ch.codePointAt(0) ?? 0);
  const n = Math.min(ca.length, cb.length);
  for (let i = 0; i < n; i += 1) {
    const x = ca[i] ?? 0;
    const y = cb[i] ?? 0;
    if (x !== y) return x - y;
  }
  return ca.length - cb.length;
}

function serializeCanonicalObject(value: Record<string, unknown>): string {
  const fields: string[] = [];
  for (const key of Object.keys(value).sort(compareCodePoints)) {
    const serialized = serializeCanonical(value[key], false);
    if (serialized === undefined) continue;
    fields.push(`${JSON.stringify(key)}:${serialized}`);
  }
  return `{${fields.join(",")}}`;
}
