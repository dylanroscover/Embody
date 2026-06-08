/// <reference types="astro/client" />
/// <reference types="@cloudflare/workers-types" />

type Runtime = import("@astrojs/cloudflare").Runtime<CloudflareEnv>;

interface CloudflareEnv {
  DB: D1Database;
  BLOBS: R2Bucket;
  KV: KVNamespace;
  TURNSTILE_SECRET: string;
  ENVIRONMENT: string;
}

declare namespace App {
  interface Locals extends Runtime {}
}
