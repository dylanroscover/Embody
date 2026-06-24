import { test, expect } from "@playwright/test";
import { ensureAdminSignedIn } from "./admin-helpers";

// Moderation queue: file a report against a seeded specimen, then triage it
// through the admin UI. Requires the local DB to be seeded (murmuration).

test("admin can see a report and move it through its status workflow", async ({ page }) => {
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");

  // File a report (the admin is also a signed-in user).
  const filed = await page.request.post("/api/specimens/murmuration/report", {
    data: { reason: "spam" }
  });
  expect([200, 201]).toContain(filed.status());

  await page.goto("/admin/reports");
  const row = page.locator("[data-row]").first();
  await expect(row).toBeVisible();

  // open -> reviewing.
  await row.locator("[data-status-select]").selectOption("reviewing");
  await row.locator("[data-apply]").click();
  await expect(row.locator("[data-status-cell]")).toHaveText("reviewing", { timeout: 10_000 });
});

test("an out-of-vocabulary report status is rejected with 400", async ({ page }) => {
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");
  const res = await page.request.post("/api/admin/reports/whatever", {
    data: { status: "bogus" }
  });
  expect(res.status()).toBe(400);
});
