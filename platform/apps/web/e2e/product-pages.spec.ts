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
