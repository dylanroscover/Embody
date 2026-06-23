import { test, expect } from "@playwright/test";
import { ensureAdminSignedIn, registerNormalUser } from "./admin-helpers";

// Security spine: pages redirect non-admins to home (existence not advertised),
// and admin APIs 404 for non-admins (indistinguishable from a missing route).

test("anonymous visitor is redirected away from /admin", async ({ page }) => {
  await page.goto("/admin");
  expect(new URL(page.url()).pathname).toBe("/");
});

test("anonymous POST to an admin API is 404 (probe-proof)", async ({ request }) => {
  const res = await request.post("/api/admin/reports/whatever", { data: { status: "open" } });
  expect(res.status()).toBe(404);
});

test("a normal signed-in user cannot reach the panel or its API", async ({ page }) => {
  await registerNormalUser(page);

  // No admin link is rendered in the nav for a non-admin.
  await page.goto("/collection");
  await expect(page.locator('a.navbar__item[href="/admin"]')).toHaveCount(0);

  // /admin redirects them to home.
  await page.goto("/admin");
  expect(new URL(page.url()).pathname).toBe("/");

  // The admin API 404s for them too (cookies shared via page.request).
  const res = await page.request.post("/api/admin/reports/whatever", {
    data: { status: "open" }
  });
  expect(res.status()).toBe(404);
});

test("the bootstrap admin reaches the dashboard and sees the admin nav link", async ({ page }) => {
  await ensureAdminSignedIn(page);

  await page.goto("/admin");
  expect(new URL(page.url()).pathname).toBe("/admin");
  await expect(page.getByRole("heading", { level: 1, name: /dashboard/i })).toBeVisible();
  await expect(page.locator('a.navbar__item[href="/admin"]')).toBeVisible();
});
