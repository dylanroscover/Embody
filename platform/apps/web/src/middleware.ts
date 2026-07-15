import { env } from "cloudflare:workers";
import { defineMiddleware } from "astro:middleware";
import { getSessionUser } from "./lib/authSession";
import type { AuthEnv } from "./lib/auth";

// Old -> new route 301s for the embody.tools unify migration.
// public/_redirects covers Cloudflare Pages; this middleware guarantees the
// same redirects for the Worker (output: "server") build AND in `astro dev`,
// where _redirects is not applied. Most specific patterns first.
const REDIRECTS: Array<[RegExp, (m: RegExpMatchArray) => string]> = [
  [/^\/specimens\/search\/?$/, () => "/collection"],
  [/^\/(?:specimens\/)?field-guide\/?$/, () => "/collection"],
  [/^\/specimens\/?$/, () => "/collection"],
  [/^\/s\/([^/]+)\/?$/, (m) => `/c/${m[1]}`]
];

export const onRequest = defineMiddleware(async (context, next) => {
  const path = context.url.pathname;
  for (const [pattern, target] of REDIRECTS) {
    const match = path.match(pattern);
    if (match) {
      return context.redirect(target(match), 301);
    }
  }

  // Populate the signed-in user into Astro.locals for pages to read. The Better
  // Auth catch-all (/api/auth/*) handles its own session internally, so skip the
  // lookup there to avoid building the auth instance twice per auth request.
  context.locals.user = null;
  if (!path.startsWith("/api/auth/")) {
    const authEnv = env as unknown as AuthEnv;
    if (authEnv?.DB && authEnv.BETTER_AUTH_SECRET) {
      try {
        context.locals.user = await getSessionUser(context.request, authEnv);
      } catch (error) {
        console.error("middleware: session resolution failed", error);
      }
    }
  }

  // Edge-cache the bare, anonymous /collection at the Cloudflare colo. It SSRs
  // two D1 reads + 24 cards (~1s of server time) yet is byte-identical for every
  // signed-out visitor, changing only when a specimen is added or reacted to.
  // Cloudflare does NOT auto-cache a Worker-generated response from Cache-Control
  // alone, so we drive the Cache API explicitly. Signed-in users (live account /
  // reaction state baked into the HTML) and any filtered ?category/?author view
  // bypass entirely. The cache is per-colo and short-lived, so a new specimen
  // shows within a minute everywhere.
  const edgeCache =
    typeof caches !== "undefined" ? (caches as unknown as { default: Cache }).default : undefined;
  const cacheableCollection =
    edgeCache !== undefined &&
    context.request.method === "GET" &&
    path === "/collection" &&
    context.url.search === "" &&
    context.locals.user === null;
  const cacheKey = cacheableCollection ? new Request(`${context.url.origin}/collection`) : undefined;

  if (edgeCache && cacheKey) {
    const hit = await edgeCache.match(cacheKey);
    // A hit was stored WITH the security headers + Cache-Control below, so it can
    // be returned without re-running the page or the header pass. But Cache API
    // responses have IMMUTABLE headers, and Astro v6's prepareResponse mutates
    // response headers after middleware returns ("Can't modify immutable
    // headers") -- so re-wrap it in a fresh, mutable Response first.
    if (hit) return new Response(hit.body, hit);
  }

  const response = await next();

  // Baseline security response headers (defense-in-depth), applied to every
  // response including API JSON and served images. `nosniff` in particular
  // closes the avatar/thumbnail MIME-sniff vector where a stored Content-Type is
  // echoed back. A full Content-Security-Policy is intentionally NOT set here --
  // it needs per-page nonce/allowlist work and would break inline scripts/styles
  // and the React islands; track it as a separate hardening step.
  try {
    const h = response.headers;
    if (!h.has("X-Content-Type-Options")) h.set("X-Content-Type-Options", "nosniff");
    if (!h.has("X-Frame-Options")) h.set("X-Frame-Options", "SAMEORIGIN");
    if (!h.has("Referrer-Policy")) h.set("Referrer-Policy", "strict-origin-when-cross-origin");
    if (!h.has("Strict-Transport-Security")) {
      // No includeSubDomains/preload -- the apex is HTTPS-only behind Cloudflare,
      // but we don't want to assert anything about subdomains we don't control.
      h.set("Strict-Transport-Security", "max-age=15552000");
    }
  } catch {
    // Some responses (e.g. immutable redirect/asset responses) reject header
    // mutation; never fail a request over a defense-in-depth header.
  }

  // Store the freshly-rendered anonymous /collection (security headers already
  // applied above) for the next colo visitor. Re-wrap so the headers are mutable
  // and so .clone() can split the body stream (one copy to cache, one returned).
  // max-age=0 keeps browsers revalidating (a new specimen / a sign-in shows
  // promptly) while s-maxage lets the edge serve it; the put runs in waitUntil
  // so it never delays this response. A non-200 or a Set-Cookie response is left
  // uncached (the Cache API refuses Set-Cookie anyway).
  if (edgeCache && cacheKey && response.status === 200 && !response.headers.has("set-cookie")) {
    const out = new Response(response.body, response);
    out.headers.set("Cache-Control", "public, max-age=0, s-maxage=60, stale-while-revalidate=300");
    // Astro v6 + @astrojs/cloudflare v13: the ExecutionContext moved from
    // locals.runtime.ctx to locals.cfContext (the old getter now throws).
    const exec = (context.locals as { cfContext?: { waitUntil?: (p: Promise<unknown>) => void } })
      .cfContext;
    if (exec?.waitUntil) exec.waitUntil(edgeCache.put(cacheKey, out.clone()));
    else await edgeCache.put(cacheKey, out.clone());
    return out;
  }

  return response;
});
