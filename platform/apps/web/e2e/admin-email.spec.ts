import { test, expect } from "@playwright/test";
import { ensureAdminSignedIn } from "./admin-helpers";

// The email test-send console: gated, validates the template, and returns the
// raw send result (skipped in dev/CI where RESEND_API_KEY is unset).

test("admin email test-send validates the template and reports the result", async ({ page }) => {
  await ensureAdminSignedIn(page);

  // Valid template -> 200. In dev with no RESEND_API_KEY the send is skipped.
  const ok = await page.request.post("/api/admin/email/test", {
    data: { template: "verification" }
  });
  expect(ok.status()).toBe(200);
  const body = await ok.json();
  expect(body.template).toBe("verification");
  expect(Boolean(body.sent) || Boolean(body.skipped)).toBeTruthy();

  // Unknown template -> 400.
  const bad = await page.request.post("/api/admin/email/test", { data: { template: "nope" } });
  expect(bad.status()).toBe(400);
});

test("the email test-send route 404s for anonymous callers", async ({ request }) => {
  const res = await request.post("/api/admin/email/test", { data: { template: "verification" } });
  expect(res.status()).toBe(404);
});
