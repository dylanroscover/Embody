import { test, expect } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Read path: the Collection is the public core of the site. These assert the
// seeded first-party specimens render and their TDXN blobs are served.
const SPECIMENS = [
  "murmuration",
  "reaction-diffusion",
  "kaleidoscope",
  "noise-terrain",
  "plasma-interference",
  "mandelbulb-march",
];

// First-party author handle: AUTHOR_HANDLE in scripts/build-specimen-data.py
// ('envoy' since 9fb23435; the test still said embody.tools, field 2026-09-11).
const FIRST_PARTY_HANDLE = "envoy";

// Expected blob for a seeded slug, read from the generated seed.sql version row
// (tdn_r2_key, tdn_sha256, size_bytes) so a specimen regen cannot strand a
// hard-coded size here again (20494 went stale on 2026-08-30).
function seededBlob(slug: string): { sha256: string; size: number } {
  const seed = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "../src/server/seed.sql"),
    "utf8"
  );
  const row = new RegExp(
    `[(]'ver-${slug}', 'sp-${slug}', [0-9]+, '([0-9a-f]{64})', '([0-9a-f]{64})', ([0-9]+),`
  ).exec(seed);
  if (!row) throw new Error(`seed.sql has no version row for ${slug}`);
  return { sha256: row[2], size: Number(row[3]) };
}

test("homepage renders hero + real featured cards", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /speed of\s*thought/i })).toBeVisible();
  // Featured cards link to real specimens, not placeholders.
  await expect(page.locator('a[href^="/c/"]').first()).toBeVisible();
  const hrefs = await page.locator('a[href^="/c/"]').evaluateAll((els) =>
    els.map((e) => (e as HTMLAnchorElement).getAttribute("href"))
  );
  expect(hrefs.some((h) => SPECIMENS.some((s) => h === `/c/${s}`))).toBeTruthy();
  // No abandoned placeholder slugs leaked back in.
  expect(hrefs.some((h) => h?.includes("infinite-zoom-tunnel"))).toBeFalsy();
});

test("collection lists the seeded specimens", async ({ page }) => {
  await page.goto("/collection");
  // Cards are <article data-specimen data-slug=...> (JS nav via data-href).
  for (const slug of SPECIMENS) {
    await expect(page.locator(`[data-specimen][data-slug="${slug}"]`)).toBeVisible();
  }
});

test("collection ?author= filters the grid to one author (SSR)", async ({ page }) => {
  // The first-party seed specimens are all authored by FIRST_PARTY_HANDLE. The
  // SSR author facet (?author=<handle>) must apply on first paint -- the seeded
  // specimens stay visible and every card on the page is by that author.
  await page.goto(`/collection?author=${FIRST_PARTY_HANDLE}`);
  for (const slug of SPECIMENS) {
    await expect(page.locator(`[data-specimen][data-slug="${slug}"]`)).toBeVisible();
  }
  const handles = await page
    .locator("[data-card-grid] a.specimen-card__author .specimen-card__author-handle")
    .evaluateAll((els) => [...new Set(els.map((e) => (e.textContent || "").trim()))]);
  expect(handles).toEqual([`@${FIRST_PARTY_HANDLE}`]);
});

test("cover network graph fits the cover (no min-height clipping)", async ({ page }) => {
  // Regression: the standalone .tdxn-viewer carries min-height: 320px, which
  // `height: 100%` does NOT override. Inside a card cover (~132-164px tall) that
  // left the ReactFlow pane stuck at 320px, so fitView centered the graph in a
  // box twice the cover's height and the cover clipped the bottom away -- the
  // graph looked shoved to the bottom with a big empty top. The cover rule now
  // clears min-height; assert the pane tracks the cover instead of 320px.
  await page.goto("/collection");
  const card = page.locator('[data-specimen][data-slug="murmuration"]');
  await card.scrollIntoViewIfNeeded();
  await card.locator('[data-cover-option="network"]').click();

  const pane = card.locator(".react-flow").first();
  await expect(pane.locator(".react-flow__node").first()).toBeVisible();

  const { coverH, paneH } = await card.evaluate((el) => ({
    coverH: el.querySelector("[data-cover-shell]")!.getBoundingClientRect().height,
    paneH: el.querySelector(".react-flow")!.getBoundingClientRect().height
  }));
  // The pane must match the (short) cover, not escape to the 320px min-height.
  expect(paneH).toBeLessThan(260);
  expect(Math.abs(paneH - coverH)).toBeLessThanOrEqual(2);
});

test("specimen page renders + TDXN blob downloads", async ({ page, request }) => {
  await page.goto("/c/murmuration");
  await expect(page.getByRole("heading", { level: 1, name: /murmuration/i })).toBeVisible();

  const res = await request.get("/api/specimens/murmuration/tdn");
  expect(res.status(), "local R2 needs the seed blobs (scripts/upload-seed-blobs.sh)").toBe(200);
  const body = await res.body();
  // Content-addressed: the bytes served are exactly the blob the version row names.
  const expected = seededBlob("murmuration");
  expect(body.byteLength).toBe(expected.size);
  expect(createHash("sha256").update(body).digest("hex")).toBe(expected.sha256);
});

test("card copy puts the _embody_tdn envelope on the clipboard", async ({ page, context }) => {
  // The copy handler must construct the ClipboardItem synchronously inside
  // the click gesture and let its payload resolve from the fetch. Writing
  // with writeText AFTER the network await loses WebKit's transient
  // activation, so Safari rejected the write and the copy silently no-oped
  // (field report, macOS 2026-08-18). Chromium can't reproduce the Safari
  // failure, but this locks the end-to-end contract through the
  // ClipboardItem path: click -> POST /copy -> valid envelope on the
  // clipboard + counter bump.
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/collection");
  const card = page.locator('[data-specimen][data-slug="kaleidoscope"]');
  const button = card.locator("[data-card-copy]");
  const before = Number(await button.locator("[data-copy-count]").textContent());
  await button.click();
  await expect(button).toHaveClass(/is-copied/);
  const raw = await page.evaluate(() => navigator.clipboard.readText());
  const env = JSON.parse(raw);
  expect(env._embody_tdn).toBe(1);
  expect(env.source).toBe("embody.tools");
  expect(env.slug).toBe("kaleidoscope");
  expect(typeof env.sha256).toBe("string");
  expect(typeof env.copy_id).toBe("string");
  expect(env.tdn && typeof env.tdn).toBe("object");
  await expect(button.locator("[data-copy-count]")).toHaveText(String(before + 1));
});
