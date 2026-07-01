import { test, expect } from "@playwright/test";

// Account lifecycle. In dev (no RESEND_API_KEY) email verification is skipped,
// so register signs the user straight in. Each run uses a unique email.
const pw = "e2e-passw0rd";
const newEmail = () => `e2e-${Date.now()}-${Math.floor(performance.now())}@example.test`;

test("register creates an account and signs in", async ({ page }) => {
  const email = newEmail();
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Tester");
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();

  // Success redirects away from /signin and the nav shows the signed-in account
  // chip -- the avatar + name button that opens the account menu. The filled
  // --register class now belongs to the "contribute" CTA, so assert the chip by
  // its accessible name (aria-label "Account menu for ...") instead.
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
  await expect(page.getByRole("button", { name: /account menu/i })).toBeVisible();
});

test("sign out, then sign back in", async ({ page }) => {
  const email = newEmail();
  // register
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Tester");
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });

  // sign out: the control lives in the nav account menu (a closed dropdown), so
  // open it first, then click sign-out -> awaits signOut() then -> "/"
  await page.goto("/contribute");
  await page.locator("[data-user-menu-toggle]").click();
  await Promise.all([
    page.waitForURL((u) => new URL(u).pathname === "/", { timeout: 15_000 }),
    page.locator("[data-user-signout]").click(),
  ]);
  // signed out: the gated /contribute now bounces anonymous users to /signin
  await page.goto("/contribute");
  await expect(page).toHaveURL(/\/signin/, { timeout: 15_000 });

  // sign back in
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
  await expect(page.getByRole("button", { name: /account menu/i })).toBeVisible();
});

test("wrong password is rejected", async ({ page }) => {
  await page.goto("/signin");
  await page.locator('input[name="email"]').fill("nobody@example.test");
  await page.locator('input[name="password"]').fill("definitely-wrong");
  await page.locator("[data-auth-submit]").click();
  await expect(page.locator("[data-auth-error]")).toBeVisible({ timeout: 15_000 });
  await expect(page).toHaveURL(/\/signin/);
});
