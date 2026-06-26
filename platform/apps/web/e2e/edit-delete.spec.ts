import { test, expect, type Page } from "@playwright/test";

// Owner submission lifecycle: edit metadata, delete, and the ownership gate.
const pw = "e2e-passw0rd";
const newEmail = () => `e2e-${Date.now()}-${Math.floor(performance.now())}@example.test`;

async function registerAndSubmit(page: Page): Promise<{ slug: string; stamp: number }> {
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Owner");
  await page.locator('input[name="email"]').fill(newEmail());
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });

  const stamp = Date.now();
  const tdn = JSON.stringify({ name: "e2e_owned", type: "baseCOMP", children: [] }, null, 2);
  await page.goto("/contribute");
  await page.locator('input[name="title"]').fill(`E2E Owned ${stamp}`);
  await page.locator('textarea[name="tdn"]').fill(tdn);
  await page.locator("[data-submit-go]").click();
  await expect(page).toHaveURL(/\/c\/e2e-owned-/, { timeout: 25_000 });
  const slug = new URL(page.url()).pathname.split("/").filter(Boolean).pop() as string;
  return { slug, stamp };
}

test("owner sees edit/delete controls and can edit metadata", async ({ page }) => {
  const { slug, stamp } = await registerAndSubmit(page);

  // Owner controls render on the specimen page.
  await expect(page.locator("[data-owner-actions]")).toBeVisible();
  await expect(page.locator("[data-delete-specimen]")).toBeVisible();

  // Edit the title + description.
  await page.goto(`/c/${slug}/edit`);
  const newTitle = `E2E Edited ${stamp}`;
  await page.locator('input[name="title"]').fill(newTitle);
  await page.locator('textarea[name="description"]').fill("Edited via e2e.");
  await page.locator("[data-edit-go]").click();

  // Redirects back to the specimen; the new title is live.
  await expect(page).toHaveURL(new RegExp(`/c/${slug}$`), { timeout: 15_000 });
  await expect(
    page.getByRole("heading", { level: 1, name: new RegExp(newTitle, "i") })
  ).toBeVisible();
});

test("owner can delete a specimen", async ({ page, request }) => {
  const { slug } = await registerAndSubmit(page);

  page.on("dialog", (d) => d.accept()); // accept the confirm() prompt
  await page.locator("[data-delete-specimen]").click();

  // Redirects to the owner's profile, and the specimen is gone from the API.
  await expect(page).toHaveURL(/\/u\//, { timeout: 15_000 });
  const res = await request.get(`/api/specimens/${slug}`);
  expect(res.status()).toBe(404);
});

test("anonymous DELETE is rejected and the specimen survives", async ({ request }) => {
  // A seeded specimen; an unauthenticated DELETE must not remove it.
  const res = await request.delete("/api/specimens/murmuration");
  expect([401, 403]).toContain(res.status());
  const check = await request.get("/api/specimens/murmuration");
  expect(check.status()).toBe(200);
});

test("non-owner is redirected away from the edit page", async ({ page }) => {
  // Register user A and submit, then register user B and try to open A's edit page.
  const { slug } = await registerAndSubmit(page);

  // Sign out, then register a different user (B). The sign-out control lives in
  // the nav account menu (a closed dropdown) -- open it before clicking.
  await page.goto("/contribute");
  await page.locator("[data-user-menu-toggle]").click();
  await Promise.all([
    page.waitForURL((u) => new URL(u).pathname === "/", { timeout: 15_000 }),
    page.locator("[data-user-signout]").click()
  ]);
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Other");
  await page.locator('input[name="email"]').fill(newEmail());
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });

  // B opening A's edit page is bounced to the read-only specimen page.
  await page.goto(`/c/${slug}/edit`);
  await expect(page).toHaveURL(new RegExp(`/c/${slug}$`), { timeout: 15_000 });
  await expect(page.locator("[data-owner-actions]")).toHaveCount(0);
});
