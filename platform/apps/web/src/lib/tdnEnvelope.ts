import {
  EMBODY_TDN_MARKER,
  EMBODY_TDN_VERSION,
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
  const serialized = serializeJsonValue(value, false);
  if (serialized === undefined) {
    throw new TypeError("TDN payload must be JSON serializable.");
  }
  return serialized;
}

function serializeJsonValue(value: unknown, inArray: boolean): string | undefined {
  if (value === null) return "null";

  switch (typeof value) {
    case "string":
      return JSON.stringify(value);
    case "number":
      return serializeNumber(value);
    case "boolean":
      return value ? "true" : "false";
    case "object":
      if (Array.isArray(value)) {
        return `[${value
          .map((item) => serializeJsonValue(item, true) ?? "null")
          .join(",")}]`;
      }
      return serializeObject(value as Record<string, unknown>);
    case "undefined":
    case "function":
    case "symbol":
      return inArray ? "null" : undefined;
    case "bigint":
      throw new TypeError("TDN payload cannot contain bigint values.");
    default:
      return undefined;
  }
}

function serializeObject(value: Record<string, unknown>): string {
  const fields: string[] = [];

  for (const key of Object.keys(value).sort()) {
    const serialized = serializeJsonValue(value[key], false);
    if (serialized === undefined) continue;
    fields.push(`${JSON.stringify(key)}:${serialized}`);
  }

  return `{${fields.join(",")}}`;
}

function serializeNumber(value: number): string {
  if (!Number.isFinite(value)) {
    throw new TypeError("TDN payload cannot contain non-finite numbers.");
  }

  const json = JSON.stringify(value);
  if (json === undefined) {
    throw new TypeError("TDN payload contains an unsupported number.");
  }

  if (!json.includes(".") && !/[eE]/.test(json)) {
    return json;
  }

  if (/[eE]/.test(json)) {
    return normalizePythonExponent(json);
  }

  if (value !== 0 && Math.abs(value) < 0.0001) {
    return fixedDecimalToPythonExponent(json);
  }

  return json;
}

function normalizePythonExponent(value: string): string {
  return value.toLowerCase().replace(/e([+-])(\d)$/i, (_match, sign: string, digit: string) => {
    return `e${sign}0${digit}`;
  });
}

function fixedDecimalToPythonExponent(value: string): string {
  const sign = value.startsWith("-") ? "-" : "";
  const unsigned = sign ? value.slice(1) : value;
  const [, fraction = ""] = unsigned.split(".");
  const firstSignificant = fraction.search(/[1-9]/);

  if (firstSignificant < 0) return "0";

  const significant = fraction.slice(firstSignificant).replace(/0+$/, "");
  const mantissa =
    significant.length === 1 ? significant : `${significant[0]}.${significant.slice(1)}`;
  const exponent = String(firstSignificant + 1).padStart(2, "0");

  return `${sign}${mantissa}e-${exponent}`;
}
