/**
 * Contract C1 canonical-serialization parity.
 *
 * The clipboard envelope's sha256 is taken over a canonical serialization of
 * the tdn payload. Python (TDXNExt.canonical_tdn_bytes) and TypeScript
 * (contracts/envelope.ts canonicalTdnString) hashed the SAME specimen to
 * different digests until 2026-08-30: Python printed whole floats as `1.0`,
 * `-0.0` and `1e+16`, which a parsed JSON payload can never reproduce. Both
 * sides now use JavaScript Number::toString rules and code-point key order;
 * this corpus pins them to identical strings.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { canonicalTdnString } from "@embody/contracts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "..", "..", "contracts", "fixtures", "canonical_cases.json");

interface Case {
  name: string;
  value: unknown;
  expected: string;
}

describe("C1 canonical serialization parity", () => {
  const doc = JSON.parse(readFileSync(FIXTURE, "utf8")) as { cases: Case[] };

  it("has a corpus", () => {
    expect(doc.cases.length).toBeGreaterThanOrEqual(30);
  });

  for (const c of doc.cases) {
    it(`matches python: ${c.name}`, () => {
      expect(canonicalTdnString(c.value)).toBe(c.expected);
    });
  }

  it("refuses non-finite numbers like the python side", () => {
    expect(() => canonicalTdnString({ a: Number.NaN })).toThrow();
    expect(() => canonicalTdnString({ a: Number.POSITIVE_INFINITY })).toThrow();
  });
});
