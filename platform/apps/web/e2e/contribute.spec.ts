import { test, expect } from "@playwright/test";
import { fillTdn } from "./editor";

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

test("anonymous user is gated from /contribute", async ({ page }) => {
  await page.goto("/contribute");
  await expect(page).toHaveURL(/\/signin/, { timeout: 15_000 });
});

test("signed-in user submits a specimen and lands on its page", async ({ page }) => {
  await register(page);

  const stamp = Date.now();
  const title = `E2E Net ${stamp}`;
  const tdn = JSON.stringify({ name: "e2e_net", type: "baseCOMP", children: [] }, null, 2);

  await page.goto("/contribute");
  await page.locator('input[name="title"]').fill(title);
  await page.locator('textarea[name="description"]').fill("An e2e-submitted test network.");
  await fillTdn(page, tdn);
  await page.locator("[data-submit-go]").click();

  // On success the client redirects to /c/<new-slug>.
  await expect(page).toHaveURL(/\/c\/e2e-net-/, { timeout: 25_000 });
  await expect(page.getByRole("heading", { level: 1, name: new RegExp(`E2E Net ${stamp}`, "i") })).toBeVisible();
});

test("signed-in user submits with multiple categories", async ({ page }) => {
  await register(page);

  const stamp = Date.now();
  const title = `E2E Multicat ${stamp}`;
  const tdn = JSON.stringify({ name: "e2e_multicat", type: "baseCOMP", children: [] }, null, 2);

  await page.goto("/contribute");
  await page.locator('input[name="title"]').fill(title);
  await fillTdn(page, tdn);

  // Open the categories picker (the .msdd holding name="categories") and add two
  // more on top of the default-checked "generative".
  await page.locator('.msdd:has(input[name="categories"]) [data-msdd-toggle]').click();
  await page.locator('input[name="categories"][value="3d"]').check({ force: true });
  await page.locator('input[name="categories"][value="shaders"]').check({ force: true });

  await page.locator("[data-submit-go]").click();
  await expect(page).toHaveURL(/\/c\/e2e-multicat-/, { timeout: 25_000 });

  // The detail breadcrumb links every category (primary first).
  await expect(page.getByRole("link", { name: "generative", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "3d", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "shaders", exact: true })).toBeVisible();
});

test("category picker caps the selection at three", async ({ page }) => {
  await register(page);
  await page.goto("/contribute");

  // "generative" is pre-checked. Add two more to reach the cap of 3.
  await page.locator('.msdd:has(input[name="categories"]) [data-msdd-toggle]').click();
  await page.locator('input[name="categories"][value="3d"]').check({ force: true });
  await page.locator('input[name="categories"][value="shaders"]').check({ force: true });

  // At the cap, every other (unchecked) category box is disabled.
  await expect(page.locator('input[name="categories"][value="video"]')).toBeDisabled();
  // Unchecking one re-enables the rest.
  await page.locator('input[name="categories"][value="3d"]').uncheck({ force: true });
  await expect(page.locator('input[name="categories"][value="video"]')).toBeEnabled();
});

test("invalid TDN is rejected", async ({ page }) => {
  await register(page);
  await page.goto("/contribute");
  await page.locator('input[name="title"]').fill(`Bad TDN ${Date.now()}`);
  await fillTdn(page, "{ not valid json");
  // Invalid TDN keeps the submit button in the not-ready (aria-disabled) state.
  await expect(page.locator("[data-submit-go]")).toHaveAttribute("aria-disabled", "true");
  // It is aria-disabled (not the `disabled` attribute), so a real click still
  // fires and surfaces a status; force past Playwright's actionability gate.
  await page.locator("[data-submit-go]").click({ force: true });
  await expect(page.locator("[data-submit-status]")).toBeVisible({ timeout: 10_000 });
  await expect(page).toHaveURL(/\/contribute/); // stayed put
});
