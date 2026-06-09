import { defineMiddleware } from "astro:middleware";

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

export const onRequest = defineMiddleware((context, next) => {
  const path = context.url.pathname;
  for (const [pattern, target] of REDIRECTS) {
    const match = path.match(pattern);
    if (match) {
      return context.redirect(target(match), 301);
    }
  }
  return next();
});
