import { test, expect } from "@playwright/test";

// Emoji reactions on a specimen (POST /api/specimens/:slug/react).
const pw = "e2e-passw0rd";
const newEmail = () => `e2e-${Date.now()}-${Math.floor(performance.now())}@example.test`;

async function register(page: import("@playwright/test").Page) {
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Reactor");
  await page.locator('input[name="email"]').fill(newEmail());
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
}

test("anonymous reaction bounces to sign in", async ({ page }) => {
  await page.goto("/c/murmuration");
  // Opening the reaction picker while signed out routes straight to /signin --
  // the popover never renders for anonymous users (reactionsClient.ts:165).
  await page.locator("[data-react-open]").click();
  await expect(page).toHaveURL(/\/signin/, { timeout: 15_000 });
});

test("signed-in user can react to a specimen", async ({ page }) => {
  await register(page);
  await page.goto("/c/murmuration");

  await page.locator("[data-react-open]").click();
  await expect(page.locator(".reaction-popover__emoji").first()).toBeVisible();
  await page.locator(".reaction-popover__emoji").first().click();

  // a "mine" reaction chip should now be present on the cluster
  await expect(page.locator(".reaction-chip.is-mine").first()).toBeVisible({ timeout: 15_000 });
});
