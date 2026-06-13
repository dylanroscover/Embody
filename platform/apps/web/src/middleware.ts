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
  [/^\/specimens\/field-guide\/?$/, () => "/field-guide"],
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

  return next();
});
