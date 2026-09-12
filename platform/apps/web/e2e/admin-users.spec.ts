import { test, expect } from "@playwright/test";
import { ensureAdminSignedIn, registerNormalUser } from "./admin-helpers";

// User management: an admin can change another user's trust_level, but cannot
// strip their own admin access (self-demote lockout).

test("admin can change another user's trust level", async ({ page }) => {
  // Create the target first (this signs THEM in), then switch to the admin.
  const email = await registerNormalUser(page);
  // registerNormalUser leaves that user signed in IN THIS CONTEXT; a signed-in
  // visitor is redirected away from /signin (signin.astro -> /u/<handle>), so
  // ensureAdminSignedIn's goto("/signin") would never show the email field.
  // Drop the session cookie first so the admin sign-in form is reachable.
  await page.context().clearCookies();
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

// The admin tables must fit their container at any width: long values truncate
// to an ellipsis and keep the full text in a title tooltip. Before this, the
// table was min-width: max-content inside an overflow-x wrapper, so one long
// email put a horizontal scrollbar under every admin table (field 2026-09-11).
test("admin tables fit their container and truncate long values", async ({ page, request, baseURL }) => {
  const long = `a-really-quite-long-address-for-truncation-${Date.now()}@example-domain-that-is-long.test`;
  await request.post("/api/auth/sign-up/email", {
    data: { email: long, password: "e2e-passw0rd", name: "Long Email Person" },
    headers: { Origin: baseURL ?? "http://127.0.0.1:4321" }
  });
  const admin = await ensureAdminSignedIn(page);
  test.skip(!admin, "admin-positive: configure ADMIN_EMAILS (or trust_level=admin) for the e2e admin");

  for (const width of [1280, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ["/admin/users", "/admin/specimens"]) {
      await page.goto(path);
      await expect(page.locator(".admin-table")).toBeVisible();
      const overflow = await page.evaluate(() => {
        const doc = document.documentElement;
        const wrap = document.querySelector(".admin-table-wrap") as HTMLElement | null;
        return {
          page: doc.scrollWidth - doc.clientWidth,
          table: wrap ? wrap.scrollWidth - wrap.clientWidth : 0
        };
      });
      expect(overflow.page, `${path} at ${width}px scrolls the page`).toBeLessThanOrEqual(1);
      expect(overflow.table, `${path} at ${width}px scrolls the table`).toBeLessThanOrEqual(1);
    }
  }

  // The long email is clipped, and its full value is one hover away.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`/admin/users?q=${encodeURIComponent(long)}`);
  const cell = page.locator("[data-row] td.trunc", { hasText: "a-really-quite-long-address" }).first();
  await expect(cell).toHaveAttribute("title", long);
  expect(await cell.evaluate((el) => el.scrollWidth > el.clientWidth + 1)).toBe(true);

  // The trust dropdown still escapes its cell rather than being clipped by it.
  const dd = page.locator(".admin-table .dd[data-dd]").first();
  await dd.locator(".dd__btn").click();
  const menu = dd.locator(".dd__menu");
  await expect(menu).toBeVisible();
  const [menuBox, cellBox] = await Promise.all([
    menu.boundingBox(),
    dd.locator("xpath=ancestor::td[1]").boundingBox()
  ]);
  expect(menuBox!.y + menuBox!.height).toBeGreaterThan(cellBox!.y + cellBox!.height);
});
