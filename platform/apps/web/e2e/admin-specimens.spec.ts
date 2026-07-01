import { test, expect } from "@playwright/test";
import { ensureAdminSignedIn } from "./admin-helpers";

// Specimen moderation: change a seeded specimen's visibility, then revert so the
// seed state (and the public Collection) is left intact. Requires a seeded DB.

test("admin can change a specimen's visibility (and revert)", async ({ page }) => {
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");

  await page.goto("/admin/specimens?q=murmuration");
  const row = page.locator("[data-row]").first();
  await expect(row).toBeVisible();

  await row.locator("[data-visibility]").selectOption("unlisted");
  await row.locator("[data-save]").click();
  await expect(page.locator("[data-status]")).toContainText(/updated/i, { timeout: 10_000 });

  // Revert to public so the rest of the suite + the public Collection are intact.
  await row.locator("[data-visibility]").selectOption("public");
  await row.locator("[data-save]").click();
  await expect(page.locator("[data-status]")).toContainText(/updated/i, { timeout: 10_000 });
});

test("an out-of-vocabulary visibility is rejected with 400", async ({ page }) => {
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");
  const res = await page.request.post("/api/admin/specimens/whatever", {
    data: { visibility: "bogus" }
  });
  expect(res.status()).toBe(400);
});
