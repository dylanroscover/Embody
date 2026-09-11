import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";

// A user's real flow: Ctrl+Shift+C in Embody puts a JSON _embody_tdn envelope
// (contract C1) on the clipboard; they paste it on /contribute and submit.
const here = dirname(fileURLToPath(import.meta.url));
const network = parseYaml(readFileSync(resolve(here, "../../../../specimens/generative/plasma-interference.tdxn"), "utf8"));
const envelope = JSON.stringify(
  { _embody_tdn: 1, source: "embody", copy_id: "e2e0000000000001", sha256: "0".repeat(64), tdn: network },
  null,
  2
);
const pw = "e2e-passw0rd";

for (const via of ["ctrl+v", "toolbar"] as const) {
  test(`pasted Embody clipboard envelope submits (${via})`, async ({ page, context, baseURL }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseURL });
    await page.goto("/signin");
    await page.locator('[data-mode="register"]').click();
    await page.locator('input[name="name"]').fill("E2E Paster");
    await page.locator('input[name="email"]').fill(`e2e-paste-${Date.now()}@example.test`);
    await page.locator('input[name="password"]').fill(pw);
    await page.locator("[data-auth-submit]").click();
    await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });

    const stamp = Date.now();
    await page.goto("/contribute");
    await page.locator('input[name="title"]').fill(`E2E Paste ${stamp}`);
    await page.evaluate((t) => navigator.clipboard.writeText(t), envelope);
    if (via === "ctrl+v") {
      await page.locator(".tdxn-editor__cm .cm-content").click();
      await page.keyboard.press("ControlOrMeta+v");
    } else {
      await page.getByRole("button", { name: "Paste TDXN from clipboard" }).click();
    }
    // Unwrapped to the bare YAML body, and the submit gate reads it as valid.
    await expect(page.locator(".tdxn-editor__cm .cm-content")).toContainText("format: tdxn");
    await expect(page.locator(".tdxn-editor__cm .cm-content")).not.toContainText("_embody_tdn");
    await expect(page.locator(".tdxn-editor")).toHaveAttribute("data-valid", "true");

    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/specimens") && r.request().method() === "POST"),
      page.locator("[data-submit-go]").click(),
    ]);
    expect(resp.status()).toBe(201);
    await expect(page).toHaveURL(/\/c\/e2e-paste-/, { timeout: 25_000 });
    await expect(page.getByRole("heading", { level: 1, name: new RegExp(`E2E Paste ${stamp}`, "i") })).toBeVisible();
  });
}
