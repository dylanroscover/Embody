/// <reference types="astro/client" />
/// <reference types="@cloudflare/workers-types" />

type Runtime = import("@astrojs/cloudflare").Runtime<CloudflareEnv>;

interface CloudflareEnv {
  DB: D1Database;
  BLOBS: R2Bucket;
  KV: KVNamespace;
  TURNSTILE_SECRET: string;
  ENVIRONMENT: string;
  // Auth (Better Auth). BETTER_AUTH_SECRET is required at runtime; the others
  // are optional. Provided via .dev.vars locally and Worker secrets in prod.
  BETTER_AUTH_SECRET?: string;
  BETTER_AUTH_URL?: string;
  GITHUB_CLIENT_ID?: string;
  GITHUB_CLIENT_SECRET?: string;
}

declare namespace App {
  interface Locals extends Runtime {
    // Populated by src/middleware.ts on every request. null when unauthenticated.
    user: import("./lib/authSession").SessionUser | null;
  }
}
