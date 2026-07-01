import { expect, type Page } from "@playwright/test";

// The e2e admin's email -- NOT a real person's address (override with
// E2E_ADMIN_EMAIL). There is no hardcoded admin: for the admin-positive specs to
// RUN rather than skip, this address must be granted admin -- add it to
// ADMIN_EMAILS in the dev server's env / .dev.vars, or give it trust_level=
// 'admin'. In dev, email verification is off, so register auto-signs-in.
export const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || "e2e-admin@example.test";
export const ADMIN_PW = "e2e-admin-passw0rd";

export const PW = "e2e-passw0rd";
export const newEmail = () => `e2e-${Date.now()}-${Math.floor(performance.now())}@example.test`;

// Sign in as the e2e admin (registering on first run), then report whether that
// account actually HAS admin access. Leaves the page on /admin when it does.
// Admin is granted only by ADMIN_EMAILS or trust_level='admin' (no hardcoded
// admin), so callers `test.skip(!returned)` when neither is configured.
export async function ensureAdminSignedIn(page: Page): Promise<boolean> {
  await page.goto("/signin");
  await page.locator('input[name="email"]').fill(ADMIN_EMAIL);
  await page.locator('input[name="password"]').fill(ADMIN_PW);
  await page.locator("[data-auth-submit]").click();
  const signedIn = await page
    .waitForURL((u) => !/\/signin/.test(u.toString()), { timeout: 5_000 })
    .then(() => true)
    .catch(() => false);
  if (!signedIn) {
    // No such account yet -> register it (auto-signs-in in dev).
    await page.goto("/signin");
    await page.locator('[data-mode="register"]').click();
    await page.locator('input[name="name"]').fill("E2E Admin");
    await page.locator('input[name="email"]').fill(ADMIN_EMAIL);
    await page.locator('input[name="password"]').fill(ADMIN_PW);
    await page.locator("[data-auth-submit]").click();
    await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
  }
  // /admin stays (200) for an admin, or redirects to / for everyone else.
  await page.goto("/admin");
  return new URL(page.url()).pathname === "/admin";
}

// Register a fresh, non-admin user and leave them signed in. Returns the email.
export async function registerNormalUser(page: Page): Promise<string> {
  const email = newEmail();
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E User");
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(PW);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
  return email;
}
