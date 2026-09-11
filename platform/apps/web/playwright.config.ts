import { defineConfig, devices } from "@playwright/test";

// E2E runs against the local dev server (astro dev -> miniflare D1/R2). In dev,
// ENVIRONMENT=development so the Turnstile gate accepts the "dev-bypass" token
// and email verification is skipped -- so signup/submit work without external
// services. Seed the local DB first: see "E2E tests" in README.md, or run
//   python3 scripts/build-specimen-data.py
//   wrangler d1 migrations apply embody --local
//   wrangler d1 execute embody --local --file ./src/server/seed.sql
//   bash scripts/upload-seed-blobs.sh
//
// E2E_PORT picks the port (default 4321). 127.0.0.1, never localhost: the dev
// server binds IPv4 only, and localhost can resolve to ::1 first.
const PORT = Number(process.env.E2E_PORT || 4321);
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  throw new Error(`E2E_PORT must be a TCP port number, got "${process.env.E2E_PORT}"`);
}
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // write tests share one local DB; keep them serial
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // CI also writes playwright-report/ (with the retry traces) for the
  // failure artifact the platform-ci e2e job uploads.
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    // Runs once before everything: resets the local D1 to a clean seed-only
    // state and provisions the e2e admin (see e2e/global.setup.ts).
    { name: "setup", testMatch: /global\.setup\.ts/ },
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      dependencies: ["setup"]
    }
  ],
  webServer: {
    command: `npm run dev -- --port ${PORT} --host 127.0.0.1`,
    url: BASE_URL,
    // Locally a server already listening on PORT is reused; CI starts fresh.
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    // Astro 7.3 backgrounds `astro dev` and exits when it detects an AI agent
    // (am-i-vibing), so Playwright would see its server die at once. This keeps
    // it in the foreground. An agent may instead start `astro dev --port P
    // --host 127.0.0.1` itself and run with E2E_PORT=P to reuse it.
    env: { ASTRO_DEV_BACKGROUND: "1" },
  },
});
