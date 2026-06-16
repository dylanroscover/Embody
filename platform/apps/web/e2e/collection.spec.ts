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

test("specimen page renders + TDN blob downloads", async ({ page, request }) => {
  await page.goto("/c/murmuration");
  await expect(page.getByRole("heading", { level: 1, name: /murmuration/i })).toBeVisible();

  const res = await request.get("/api/specimens/murmuration/tdn");
  expect(res.status()).toBe(200);
  const body = await res.body();
  expect(body.byteLength).toBe(20494); // content-addressed: matches seed.sql size
});
