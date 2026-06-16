import { defineConfig, devices } from "@playwright/test";

// E2E runs against the local dev server (astro dev -> miniflare D1/R2). In dev,
// ENVIRONMENT=development so the Turnstile gate accepts the "dev-bypass" token
// and email verification is skipped -- so signup/submit work without external
// services. Seed the local DB first: see e2e/README or run
//   python3 scripts/build-specimen-data.py
//   wrangler d1 migrations apply embody --local
//   wrangler d1 execute embody --local --file ./src/server/seed.sql
//   bash scripts/upload-seed-blobs.sh
const PORT = 4321;
const BASE_URL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // write tests share one local DB; keep them serial
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "line" : [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run dev",
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
