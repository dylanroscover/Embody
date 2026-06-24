import { test, expect } from "@playwright/test";

// User avatars: sign up a fresh account, then exercise the upload -> serve ->
// render -> validate -> remove loop. The upload is driven by an in-page authed
// fetch of a canvas-generated WebP (not the file-input UI), so the test is
// immune to dev-server client-script flakiness; the RENDER assertions are SSR.
test("avatar upload, serve, render, validation, and remove", async ({ page }) => {
  const email = `e2e-avatar-${Date.now().toString(36)}@example.com`;

  // Sign up (dev: no email provider -> auto signed in).
  await page.goto("/signin");
  await page.click('button[data-mode="register"]');
  await page.fill('input[name="name"]', "Avatar E2E");
  await page.fill('input[name="email"]', email);
  await page.fill('input[name="password"]', "sup3rsecret!");
  await page.click('button[data-auth-submit]');

  // The profile link lives in the (collapsed) account menu, so it's attached but
  // not visible until the menu opens -- we only need its href, which proves the
  // session is live.
  const profileLink = page.locator('a[href^="/u/"]').first();
  await profileLink.waitFor({ state: "attached", timeout: 15_000 });
  const href = await profileLink.getAttribute("href");
  expect(href).toBeTruthy();

  await page.goto(href!);

  // Upload a real (canvas-encoded) WebP through the authed route.
  const upload = await page.evaluate(async () => {
    const c = document.createElement("canvas");
    c.width = 256;
    c.height = 256;
    const ctx = c.getContext("2d")!;
    ctx.fillStyle = "#6ee668";
    ctx.fillRect(0, 0, 256, 256);
    const res = await fetch("/api/account/avatar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ avatar: c.toDataURL("image/webp", 0.85) })
    });
    return { status: res.status, body: (await res.json()) as { avatar_url?: string } };
  });
  expect(upload.status).toBe(200);
  expect(upload.body.avatar_url).toMatch(/^\/api\/avatars\/[a-f0-9]{64}$/);

  // The serve route returns the immutable image.
  const served = await page.request.get(upload.body.avatar_url!);
  expect(served.status()).toBe(200);
  expect(served.headers()["content-type"]).toContain("image/");
  expect(served.headers()["cache-control"]).toContain("immutable");

  // Profile + nav both render the avatar as an <img> after reload (SSR).
  await page.goto(href!);
  await expect(page.locator(".profile-avatar img[data-avatar-img]")).toHaveAttribute(
    "src",
    upload.body.avatar_url!
  );
  await expect(page.locator(".navbar__user-avatar")).toHaveJSProperty("tagName", "IMG");

  // A non-image payload is rejected.
  const bad = await page.evaluate(async () => {
    const res = await fetch("/api/account/avatar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ avatar: "data:text/plain;base64,aGVsbG8=" })
    });
    return res.status;
  });
  expect(bad).toBe(400);

  // Remove reverts to the letter chip (this account has no GitHub avatar).
  const removed = await page.evaluate(async () => {
    const res = await fetch("/api/account/avatar", { method: "DELETE" });
    return res.status;
  });
  expect(removed).toBe(200);
  await page.goto(href!);
  await expect(page.locator(".profile-avatar span.avatar--initial")).toBeVisible();
  await expect(page.locator(".profile-avatar img[data-avatar-img]")).toHaveCount(0);
});
