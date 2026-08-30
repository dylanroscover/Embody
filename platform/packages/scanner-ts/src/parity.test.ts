/**
 * Cross-implementation parity for the TDXN capability scanner (contract C8).
 *
 * SCANNER-SPEC.md requires scanner-ts and Embody/Collection/scanner.py to
 * return identical verdict + counts on a SHARED corpus. Neither fixture
 * directory existed, so the two implementations had never been compared.
 * This is the TypeScript half; dev/embody/unit_tests/test_scanner_parity.py
 * is the other, reading the same files.
 *
 * The divergence ledger fails in BOTH directions on purpose: a new
 * disagreement is a failure, and so is fixing a declared one without
 * removing its note -- so a known gap can never go quiet.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { scanTdn } from "./scanner";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "..", "fixtures");

const COUNT_KEYS = [
  "execute_dats", "file_read_exprs", "web_ops", "extensions",
  "storage_payloads", "denylisted_types", "traversal_paths", "external_refs"
] as const;

interface Fixture {
  name: string;
  why: string;
  surfaces: string[];
  tdn: Record<string, unknown>;
  expect_py: { verdict: string; counts: Record<string, number> };
  expect_ts: { verdict: string; counts: Record<string, number> };
  divergence?: string;
}

function loadFixtures(): Fixture[] {
  return readdirSync(FIXTURES)
    .filter((n) => n.endsWith(".json"))
    .sort()
    .map((n) => JSON.parse(readFileSync(join(FIXTURES, n), "utf8")) as Fixture);
}

function normalizedCounts(counts: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const k of COUNT_KEYS) out[k] = counts[k] ?? 0;
  return out;
}

describe("scanner parity corpus (C8)", () => {
  const fixtures = loadFixtures();

  it("is not empty", () => {
    expect(fixtures.length).toBeGreaterThanOrEqual(20);
  });

  it("covers every capability surface", () => {
    const seen = new Set(fixtures.flatMap((f) => f.surfaces ?? []));
    const missing = COUNT_KEYS.filter((k) => !seen.has(k));
    expect(missing, `no fixture exercises: ${missing.join(", ")}`).toEqual([]);
  });

  for (const fx of fixtures) {
    it(`typescript matches its recorded expectation: ${fx.name}`, () => {
      const result = scanTdn(fx.tdn);
      expect(result.verdict, `${fx.name}: verdict drifted`).toBe(fx.expect_ts.verdict);
      expect(normalizedCounts(result.counts as unknown as Record<string, number>),
             `${fx.name}: counts drifted`).toEqual(fx.expect_ts.counts);
    });
  }

  it("has an exact divergence ledger", () => {
    for (const fx of fixtures) {
      const same =
        fx.expect_py.verdict === fx.expect_ts.verdict &&
        JSON.stringify(fx.expect_py.counts) === JSON.stringify(fx.expect_ts.counts);
      const declared = Boolean(fx.divergence);
      if (same && declared) {
        throw new Error(
          `${fx.name}: expectations now AGREE but the fixture still declares a ` +
          `divergence -- delete the \`divergence\` field, the gap is closed.`
        );
      }
      if (!same && !declared) {
        throw new Error(
          `${fx.name}: python and typescript expectations DISAGREE with no ` +
          `\`divergence\` note. C8 requires identical verdict+counts; either fix ` +
          `the scanner or declare the gap explicitly.`
        );
      }
    }
  });
});
