import cloudflare from "@astrojs/cloudflare";
import react from "@astrojs/react";
import { defineConfig } from "astro/config";

export default defineConfig({
  output: "server",
  adapter: cloudflare({
    platformProxy: {
      enabled: true
    },
    // Optimize images at runtime via Cloudflare Images (the env.IMAGES binding
    // in wrangler.jsonc). Required because the landing is SSR, so build-time
    // optimization can't apply -- the worker transforms the hero to avif/webp
    // per request. NOTE: needs "Image Transformations" enabled for the zone in
    // the Cloudflare dashboard (free tier); without it, images serve as-is.
    imageService: "cloudflare"
  }),
  integrations: [react()]
});
