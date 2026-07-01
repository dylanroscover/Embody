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

  return response;
});
