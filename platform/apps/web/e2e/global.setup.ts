import { test as setup, expect } from "@playwright/test";
import { execSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ADMIN_EMAIL, ADMIN_PW } from "./admin-helpers";

// Runs ONCE before the whole suite (a Playwright "setup" project the chromium
// project depends on). It guarantees a deterministic starting state so the
// write-tests (which create e2e-* specimens and e2e users) cannot leak into the
// read-tests, and so the admin-positive specs actually RUN instead of skipping:
//
//   1. Reset the local miniflare D1 to a clean, seed-only state (reset.sql),
//      then re-apply the canonical seed (src/server/seed.sql).
//   2. Register the e2e admin fresh against the live dev server.
//   3. Promote that account to trust_level='admin' so isAdmin() grants it.
//
// The dev server is up but IDLE while this runs (no other test makes requests
// until this project finishes), so the wrangler D1 writes don't race a query.

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function d1(arg: string): void {
  // --local targets the same miniflare SQLite that `astro dev` uses.
  execSync(`npx wrangler d1 execute embody --local ${arg}`, {
    cwd: webRoot,
    stdio: "pipe"
  });
}

setup("reset local D1 and provision the e2e admin", async ({ request, baseURL }) => {
  // 1. Clean slate, then seed.
  d1("--file=e2e/reset.sql");
  d1("--file=src/server/seed.sql");

  // 2. Register the admin fresh (dev: no email verification, no Turnstile on the
  //    auth endpoints, rate limiting disabled -- so this just creates the row).
  const res = await request.post("/api/auth/sign-up/email", {
    data: { email: ADMIN_EMAIL, password: ADMIN_PW, name: "E2E Admin" },
    headers: { Origin: baseURL ?? "http://localhost:4321" }
  });
  expect(res.ok(), `admin sign-up failed: ${res.status()} ${await res.text()}`).toBeTruthy();

  // 3. Promote to admin so the admin-positive specs run rather than test.skip.
  const email = ADMIN_EMAIL.replace(/'/g, "''");
  d1(`--command="UPDATE users_profile SET trust_level='admin' WHERE id = (SELECT id FROM user WHERE email='${email}')"`);
});
