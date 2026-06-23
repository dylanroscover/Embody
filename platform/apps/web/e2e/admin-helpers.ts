import { expect, type Page } from "@playwright/test";

// The hard-coded bootstrap admin (DEFAULT_ADMIN_EMAIL in src/server/admin.ts) is
// ALWAYS an admin with zero config, so e2e can sign in as it without setting
// ADMIN_EMAILS or doing any DB surgery. In dev, email verification is off, so
// register auto-signs-in.
export const ADMIN_EMAIL = "rosco@tec.design";
export const ADMIN_PW = "e2e-admin-passw0rd";

export const PW = "e2e-passw0rd";
export const newEmail = () => `e2e-${Date.now()}-${Math.floor(performance.now())}@example.test`;

// Sign in as the bootstrap admin, registering the account on first run (the
// local dev DB persists, so later runs just sign in).
export async function ensureAdminSignedIn(page: Page): Promise<void> {
  await page.goto("/signin");
  await page.locator('input[name="email"]').fill(ADMIN_EMAIL);
  await page.locator('input[name="password"]').fill(ADMIN_PW);
  await page.locator("[data-auth-submit]").click();
  const signedIn = await page
    .waitForURL((u) => !/\/signin/.test(u.toString()), { timeout: 5_000 })
    .then(() => true)
    .catch(() => false);
  if (signedIn) return;

  // No such account yet -> register it (auto-signs-in in dev).
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Admin");
  await page.locator('input[name="email"]').fill(ADMIN_EMAIL);
  await page.locator('input[name="password"]').fill(ADMIN_PW);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
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
