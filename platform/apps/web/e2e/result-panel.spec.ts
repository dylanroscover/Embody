import { test, expect, type Page } from "@playwright/test";
import { registerNormalUser } from "./admin-helpers";

// The specimen page's rendered-result panel takes the artwork's aspect ratio at
// the same area as a 16:9 cover, can be resized from its top-left grip, and keeps
// that size: in localStorage for anyone, on the account for a signed-in user.
// thermite-and-freon is seeded with a 4:5 (960x1200) cover.
const SLUG = "thermite-and-freon";
const PORTRAIT = 960 / 1200;

const figure = (page: Page) => page.locator("[data-thumb-figure]");

async function figureBox(page: Page): Promise<{ w: number; h: number }> {
  const box = await figure(page).boundingBox();
  if (!box) throw new Error("rendered-result figure has no box");
  return { w: box.width, h: box.height };
}

// The panel width var(--app-panel-w) is what the properties panel is sized to; a
// 16:9 cover at the default size fills that width minus the panel padding.
async function defaultArea(page: Page): Promise<number> {
  const props = await page.locator(".specimen-app__props").boundingBox();
  if (!props) throw new Error("properties panel has no box");
  const w = props.width - 32;
  return w * w * (9 / 16);
}

async function openSpecimen(page: Page): Promise<void> {
  await page.goto(`/c/${SLUG}`);
  // The poster must have loaded: the figure only takes its aspect from it.
  await expect
    .poll(async () => {
      const { w, h } = await figureBox(page);
      return Math.abs(w / h - PORTRAIT);
    })
    .toBeLessThan(0.02);
}

async function dragGrip(page: Page, dx: number, dy: number): Promise<void> {
  const grip = page.locator("[data-thumb-resize]");
  const box = await grip.boundingBox();
  if (!box) throw new Error("resize grip has no box");
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx / 2, y + dy / 2, { steps: 4 });
  await page.mouse.move(x + dx, y + dy, { steps: 4 });
  await page.mouse.up();
}

test("the rendered result takes the artwork's aspect at the default area", async ({ page }) => {
  await openSpecimen(page);
  const { w, h } = await figureBox(page);
  // Borders make the measured box a couple of pixels larger than the content.
  expect(Math.abs(w * h / (await defaultArea(page)) - 1)).toBeLessThan(0.05);
});

test("dragging the grip resizes the panel and the size survives a reload", async ({ page }) => {
  await openSpecimen(page);
  const before = await figureBox(page);
  await dragGrip(page, -80, -80);
  const after = await figureBox(page);
  expect(after.w).toBeGreaterThan(before.w * 1.2);
  expect(Math.abs(after.w / after.h - PORTRAIT)).toBeLessThan(0.02);

  await openSpecimen(page);
  const reloaded = await figureBox(page);
  expect(Math.abs(reloaded.w - after.w)).toBeLessThan(2);

  // Double-click resets to the default size.
  await page.locator("[data-thumb-resize]").dblclick();
  expect(Math.abs((await figureBox(page)).w - before.w)).toBeLessThan(2);
});

test("a signed-in user's panel size follows the account to a fresh browser", async ({ page, browser, baseURL }) => {
  const email = await registerNormalUser(page);
  await openSpecimen(page);
  const before = await figureBox(page);
  const saved = page.waitForResponse((r) => r.url().endsWith("/api/account/preferences") && r.request().method() === "POST");
  await dragGrip(page, -60, -60);
  expect((await saved).ok()).toBeTruthy();
  const resized = await figureBox(page);
  expect(resized.w).toBeGreaterThan(before.w * 1.1);

  // A new context has no localStorage: only the account can carry the size over.
  const fresh = await browser.newContext({ baseURL });
  const other = await fresh.newPage();
  await other.goto("/signin");
  await other.locator('input[name="email"]').fill(email);
  await other.locator('input[name="password"]').fill("e2e-passw0rd");
  await other.locator("[data-auth-submit]").click();
  await expect(other).not.toHaveURL(/\/signin/, { timeout: 15_000 });
  await openSpecimen(other);
  expect(Math.abs((await figureBox(other)).w - resized.w)).toBeLessThan(2);
  await fresh.close();
});

test("the TDXN source highlights each DAT's code in its own language", async ({ page }) => {
  await page.goto(`/c/${SLUG}`);
  await page.keyboard.press("t");
  const source = page.locator("#tdxn-panel");
  await expect(source).not.toHaveClass(/is-collapsed/);
  // Every code block is labelled with its language beside `dat_content: |`.
  await expect(source.locator(".tdxn-yaml__lang", { hasText: "glsl" }).first()).toBeVisible();
  await expect(source.locator(".tdxn-yaml__lang", { hasText: "python" }).first()).toBeAttached();
  // GLSL keywords and Python keywords take the keyword class inside those blocks.
  await expect(source.locator(".tdxn-yaml__kw", { hasText: /^uniform$/ }).first()).toBeAttached();
  await expect(source.locator(".tdxn-yaml__kw", { hasText: /^def$/ }).first()).toBeAttached();
  // TouchDesigner GLSL builtins read in the accent.
  await expect(source.locator(".tdxn-yaml__expr", { hasText: /^sTD2DInputs$/ }).first()).toBeAttached();
});
