import { test, expect } from "@playwright/test";
import { ensureAdminSignedIn, registerNormalUser } from "./admin-helpers";

// User management: an admin can change another user's trust_level, but cannot
// strip their own admin access (self-demote lockout).

test("admin can change another user's trust level", async ({ page }) => {
  // Create the target first (this signs THEM in), then switch to the admin.
  const email = await registerNormalUser(page);
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");

  await page.goto(`/admin/users?q=${encodeURIComponent(email)}`);
  const row = page.locator("[data-row]").first();
  await expect(row).toBeVisible();

  await row.locator("[data-trust]").selectOption("curator");
  await row.locator("[data-save]").click();
  await expect(page.locator("[data-status]")).toContainText(/curator/i, { timeout: 10_000 });
});

test("admin cannot demote their own admin access (self-demote -> 409)", async ({ page }) => {
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");

  // Resolve the admin's own id from the live session (cookies shared via page.request).
  const session = await page.request.get("/api/auth/get-session").then((r) => r.json());
  const id = session?.user?.id as string | undefined;
  expect(id).toBeTruthy();

  const res = await page.request.post(`/api/admin/users/${id}`, {
    data: { trustLevel: "verified" }
  });
  expect(res.status()).toBe(409);
});

test("an out-of-vocabulary trust level is rejected with 400", async ({ page }) => {
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");
  const res = await page.request.post("/api/admin/users/whatever", {
    data: { trustLevel: "wizard" }
  });
  expect(res.status()).toBe(400);
});
