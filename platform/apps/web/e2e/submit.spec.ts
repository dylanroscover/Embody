import { test, expect } from "@playwright/test";

// The core write path: a signed-in user submits a Specimen. In dev,
// ENVIRONMENT=development so the Turnstile gate accepts the dev-bypass path.
const pw = "e2e-passw0rd";
const newEmail = () => `e2e-${Date.now()}-${Math.floor(performance.now())}@example.test`;

async function register(page: import("@playwright/test").Page) {
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Author");
  await page.locator('input[name="email"]').fill(newEmail());
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
}

test("anonymous user is gated from /submit", async ({ page }) => {
  await page.goto("/submit");
  await expect(page).toHaveURL(/\/signin/, { timeout: 15_000 });
});

test("signed-in user submits a specimen and lands on its page", async ({ page }) => {
  await register(page);

  const stamp = Date.now();
  const title = `E2E Net ${stamp}`;
  const tdn = JSON.stringify({ name: "e2e_net", type: "baseCOMP", children: [] }, null, 2);

  await page.goto("/submit");
  await page.locator('input[name="title"]').fill(title);
  await page.locator('textarea[name="description"]').fill("An e2e-submitted test network.");
  await page.locator('textarea[name="tdn"]').fill(tdn);
  await page.locator("[data-submit-go]").click();

  // On success the client redirects to /c/<new-slug>.
  await expect(page).toHaveURL(/\/c\/e2e-net-/, { timeout: 25_000 });
  await expect(page.getByRole("heading", { level: 1, name: new RegExp(`E2E Net ${stamp}`, "i") })).toBeVisible();
});

test("invalid TDN is rejected", async ({ page }) => {
  await register(page);
  await page.goto("/submit");
  await page.locator('input[name="title"]').fill(`Bad TDN ${Date.now()}`);
  await page.locator('textarea[name="tdn"]').fill("{ not valid json");
  await page.locator("[data-submit-go]").click();
  await expect(page.locator("[data-submit-status]")).toBeVisible({ timeout: 10_000 });
  await expect(page).toHaveURL(/\/submit/); // stayed put
});
