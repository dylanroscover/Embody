import { test, expect } from "@playwright/test";

// The product pages, ported from the retired web/ deploy (issue #156): they
// existed only on www.embody.tools, so search sent people to a stale http copy.
const PAGES: Array<[string, RegExp]> = [
  ["/tdxn/", /^TDXN/],
  ["/embody/", /^Embody/],
  ["/envoy/", /^Envoy/]
];

for (const [path, title] of PAGES) {
  test(`${path} is served by the live site`, async ({ page }) => {
    const response = await page.goto(path);
    expect(response?.status()).toBe(200);
    await expect(page).toHaveTitle(title);
  });
}

test("the landing's three pillars link to the product pages", async ({ page }) => {
  await page.goto("/");
  const pillars = page.locator(".section--pillars a.pillar--link");
  await expect(pillars).toHaveCount(PAGES.length);
  for (const [path] of PAGES) {
    await expect(page.locator(`.section--pillars a[href="${path}"]`)).toHaveCount(1);
  }
});
