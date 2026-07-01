import { test, expect } from "@playwright/test";

// Read path: the Collection is the public core of the site. These assert the
// seeded first-party specimens render and their TDN blobs are served.
const SPECIMENS = [
  "murmuration",
  "reaction-diffusion",
  "kaleidoscope",
  "noise-terrain",
  "plasma-interference",
  "mandelbulb-march",
];

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
  // The first-party seed specimens are all authored by embody.tools. The SSR
  // author facet (?author=<handle>) must apply on first paint -- the seeded
  // specimens stay visible and every card on the page is by that author.
  await page.goto("/collection?author=embody.tools");
  for (const slug of SPECIMENS) {
    await expect(page.locator(`[data-specimen][data-slug="${slug}"]`)).toBeVisible();
  }
  const handles = await page
    .locator("[data-card-grid] a.specimen-card__author .specimen-card__author-handle")
    .evaluateAll((els) => [...new Set(els.map((e) => (e.textContent || "").trim()))]);
  expect(handles).toEqual(["@embody.tools"]);
});

test("cover network graph fits the cover (no min-height clipping)", async ({ page }) => {
  // Regression: the standalone .tdn-viewer carries min-height: 320px, which
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

test("specimen page renders + TDN blob downloads", async ({ page, request }) => {
  await page.goto("/c/murmuration");
  await expect(page.getByRole("heading", { level: 1, name: /murmuration/i })).toBeVisible();

  const res = await request.get("/api/specimens/murmuration/tdn");
  expect(res.status()).toBe(200);
  const body = await res.body();
  expect(body.byteLength).toBe(20494); // content-addressed: matches seed.sql size
});
