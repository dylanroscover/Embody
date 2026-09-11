import { test, expect, type Page, type Route } from "@playwright/test";

// Account lifecycle. In dev (no RESEND_API_KEY) email verification is skipped,
// so register signs the user straight in. Each run uses a unique email.
const pw = "e2e-passw0rd";
const newEmail = () => `e2e-${Date.now()}-${Math.floor(performance.now())}@example.test`;

async function fillSignIn(page: Page, email = "nobody@example.test", password = "definitely-wrong") {
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
}

test("register creates an account and signs in", async ({ page }) => {
  const email = newEmail();
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Tester");
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();

  // Success redirects away from /signin and the nav shows the signed-in account
  // chip -- the avatar + name button that opens the account menu. The filled
  // --register class now belongs to the "contribute" CTA, so assert the chip by
  // its accessible name (aria-label "Account menu for ...") instead.
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
  await expect(page.getByRole("button", { name: /account menu/i })).toBeVisible();
});

test("sign out, then sign back in", async ({ page }) => {
  const email = newEmail();
  // register
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Tester");
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });

  // sign out: the control lives in the nav account menu (a closed dropdown), so
  // open it first, then click sign-out -> awaits signOut() then -> "/"
  await page.goto("/contribute");
  await page.locator("[data-user-menu-toggle]").click();
  await Promise.all([
    page.waitForURL((u) => new URL(u).pathname === "/", { timeout: 15_000 }),
    page.locator("[data-user-signout]").click(),
  ]);
  // signed out: the gated /contribute now bounces anonymous users to /signin
  await page.goto("/contribute");
  await expect(page).toHaveURL(/\/signin/, { timeout: 15_000 });

  // sign back in
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(pw);
  await page.locator("[data-auth-submit]").click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 15_000 });
  await expect(page.getByRole("button", { name: /account menu/i })).toBeVisible();
});

// Pins the STATUS, not just a visible error: during the 2026-09-11 outage every
// /api/auth/* was an empty 500 and this box still read "invalid email or password.".
test("wrong password is rejected with a 401 and the credential message", async ({ page }) => {
  await page.goto("/signin");
  await fillSignIn(page);
  const [res] = await Promise.all([
    page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/auth/sign-in/email" && r.request().method() === "POST",
      { timeout: 15_000 }
    ),
    page.locator("[data-auth-submit]").click()
  ]);
  expect(res.status()).toBe(401);
  await expect(page.locator("[data-auth-error]")).toHaveText(/invalid email or password/i, { timeout: 15_000 });
  await expect(page).toHaveURL(/\/signin/);
});

// --- server failure copy (field 2026-09-11) ---------------------------------
// A 5xx, a non-JSON body, or a dropped connection must read as "unavailable",
// never as a credential or link problem, and must hand the button back.
// page.route stubs the endpoint, so these pin the client copy; the happy path
// and the canary below pin the real server.

const outages: Array<{ name: string; stub: (route: Route) => Promise<void> }> = [
  { name: "500 with an empty body", stub: (r) => r.fulfill({ status: 500, body: "" }) },
  { name: "502 with an html body", stub: (r) => r.fulfill({ status: 502, contentType: "text/html", body: "<h1>bad gateway</h1>" }) },
  { name: "network failure", stub: (r) => r.abort("failed") }
];

for (const { name, stub } of outages) {
  test(`sign-in ${name} reads as unavailable, not a bad password`, async ({ page }) => {
    await page.route("**/api/auth/sign-in/email", stub);
    await page.goto("/signin");
    await fillSignIn(page);
    const submit = page.locator("[data-auth-submit]");
    await submit.click();
    const err = page.locator("[data-auth-error]");
    await expect(err).toHaveText(/sign-in is unavailable right now/, { timeout: 15_000 });
    await expect(err).not.toContainText(/invalid email or password/i);
    await expect(submit).toBeEnabled();
    await expect(submit).toHaveText("sign in");
    await expect(page).toHaveURL(/\/signin/);
  });
}

test("sign-in rate limit reads as slow down, not a bad password", async ({ page }) => {
  // The catch-all limiter answers { error, detail } with no `message`.
  await page.route("**/api/auth/sign-in/email", (r) =>
    r.fulfill({
      status: 429,
      contentType: "application/json",
      body: JSON.stringify({ error: "rate_limited", detail: "Too many attempts. Please slow down and try again shortly." })
    })
  );
  await page.goto("/signin");
  await fillSignIn(page);
  await page.locator("[data-auth-submit]").click();
  const err = page.locator("[data-auth-error]");
  await expect(err).toHaveText(/too many attempts/, { timeout: 15_000 });
  await expect(err).not.toContainText(/invalid email or password/i);
});

// Only Better Auth code EMAIL_NOT_VERIFIED means "verify your email"; any other
// 403 is a server fault and must not send the user to their inbox (field 2026-09-11).
for (const { code, verify } of [
  { code: "EMAIL_NOT_VERIFIED", verify: true },
  { code: "INVALID_ORIGIN", verify: false }
]) {
  test(`sign-in 403 ${code} ${verify ? "asks to verify" : "is not read as unverified"}`, async ({ page }) => {
    await page.route("**/api/auth/sign-in/email", (r) =>
      r.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ code, message: code.toLowerCase() }) })
    );
    await page.goto("/signin");
    await fillSignIn(page);
    await page.locator("[data-auth-submit]").click();
    const err = page.locator("[data-auth-error]");
    await expect(err).toBeVisible({ timeout: 15_000 });
    if (verify) {
      await expect(err).toContainText("verified yet");
      await expect(page.locator("[data-resend]")).toBeVisible();
    } else {
      await expect(err).not.toContainText("verified");
      await expect(page.locator("[data-resend]")).toBeHidden();
    }
  });
}

test("register outage reads as unavailable", async ({ page }) => {
  await page.route("**/api/auth/sign-up/email", (r) => r.fulfill({ status: 500, body: "" }));
  await page.goto("/signin");
  await page.locator('[data-mode="register"]').click();
  await page.locator('input[name="name"]').fill("E2E Tester");
  await fillSignIn(page, newEmail(), pw);
  const submit = page.locator("[data-auth-submit]");
  await submit.click();
  const err = page.locator("[data-auth-error]");
  await expect(err).toHaveText(/registration is unavailable right now/, { timeout: 15_000 });
  await expect(err).not.toContainText("could not create the account");
  await expect(submit).toBeEnabled();
  await expect(submit).toHaveText("create account");
});

test("forgot-password outage reads as unavailable, not a failed send", async ({ page }) => {
  await page.route("**/api/auth/request-password-reset", (r) => r.fulfill({ status: 500, body: "" }));
  await page.goto("/forgot-password");
  await page.locator('input[name="email"]').fill("nobody@example.test");
  const submit = page.locator("[data-forgot-submit]");
  await submit.click();
  const err = page.locator("[data-auth-error]");
  await expect(err).toHaveText(/password reset is unavailable right now/, { timeout: 15_000 });
  await expect(err).not.toContainText("could not send the reset link");
  await expect(page.locator("[data-auth-notice]")).toHaveText("");
  await expect(submit).toBeEnabled();
  await expect(submit).toHaveText("send reset link");
});

test("reset-password outage reads as unavailable, not an expired link", async ({ page }) => {
  await page.route("**/api/auth/reset-password", (r) => r.fulfill({ status: 500, body: "" }));
  await page.goto("/reset-password?token=e2e-bogus-token");
  await page.locator('input[name="password"]').fill(pw);
  await page.locator('input[name="confirm"]').fill(pw);
  const submit = page.locator("[data-reset-submit]");
  await submit.click();
  const err = page.locator("[data-auth-error]");
  await expect(err).toHaveText(/password reset is unavailable right now/, { timeout: 15_000 });
  await expect(err).not.toContainText("expired");
  await expect(submit).toBeEnabled();
  await expect(submit).toHaveText("set new password");
});

// --- real server ------------------------------------------------------------

test("forgot-password for a real account returns 200 and the neutral confirmation", async ({ page, request, baseURL }) => {
  // Own cookie jar (the `request` fixture), so the page stays signed out --
  // /forgot-password redirects a signed-in user. A real account walks the
  // verification-row write; with no RESEND_API_KEY sendEmail no-ops.
  const email = newEmail();
  const signUp = await request.post("/api/auth/sign-up/email", {
    data: { email, password: pw, name: "E2E Reset" },
    headers: { Origin: baseURL! },
    timeout: 15_000
  });
  expect(signUp.status(), await signUp.text()).toBe(200);

  await page.goto("/forgot-password");
  await page.locator('input[name="email"]').fill(email);
  const [res] = await Promise.all([
    page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/auth/request-password-reset" && r.request().method() === "POST",
      { timeout: 15_000 }
    ),
    page.locator("[data-forgot-submit]").click()
  ]);
  expect(res.status()).toBe(200);
  await expect(page.locator("[data-auth-notice]")).toContainText("a reset link is on its way", { timeout: 15_000 });
  await expect(page.locator("[data-auth-error]")).toHaveText("");
});

test("auth API canary: get-session 200, bogus sign-in 401", async ({ request, baseURL }) => {
  // Better Auth 1.7's schema check 500'd every /api/auth/* on D1 with an empty
  // body (field 2026-09-11); the deploy job's prod smoke runs the same two
  // probes. Origin mirrors global.setup.ts: Better Auth checks it on POSTs that
  // carry one (or a cookie) against the request's own origin.
  const session = await request.get("/api/auth/get-session", { timeout: 15_000 });
  expect(session.status(), await session.text()).toBe(200);
  expect(await session.json()).toBeNull();

  const signIn = await request.post("/api/auth/sign-in/email", {
    data: { email: "nobody@example.test", password: "definitely-wrong" },
    headers: { Origin: baseURL! },
    timeout: 15_000
  });
  expect(signIn.status(), await signIn.text()).toBe(401);
  expect((await signIn.json()).code).toBe("INVALID_EMAIL_OR_PASSWORD");
});
