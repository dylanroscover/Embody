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
  integrations: [react()],
  vite: {
    // Keep a single React instance across the module graph (good hygiene for the
    // monorepo). The real fix for the React Flow SSR crash is rendering TdnViewer
    // client:only (it is a DOM-only canvas that must not be server-rendered) --
    // see the TdnViewer mounts in index.astro and c/[slug].astro.
    resolve: {
      dedupe: ["react", "react-dom"]
    },
    // Pre-bundle the React-using deps together so the dev optimizer never splits
    // React into two module instances. Without this, a dependency-graph change
    // can leave a stale .vite bundle where @xyflow/react (pulled in by the
    // @embody/tdn-viewer workspace package) carries a second React -- every
    // client:load island then throws "Invalid hook call / more than one copy of
    // React" on hydrate, so the SSR'd markup flashes and vanishes. dedupe alone
    // does not cover this in dev; explicit include does. (Prod/rollup already
    // dedupes at build, so this is a dev-server safeguard.)
    optimizeDeps: {
      include: ["react", "react-dom", "@xyflow/react"]
    }
  }
});
