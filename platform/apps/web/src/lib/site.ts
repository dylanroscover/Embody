// Canonical site identity for absolute OG / canonical URLs.
//
// SITE_ORIGIN is hardcoded (not derived from Astro.url) on purpose: a link
// shared from a preview deploy or localhost must still point social scrapers at
// production, never at the preview host. Keep this in sync with the deployed
// domain.
export const SITE_ORIGIN = "https://embody.tools";

// Default social-share image: the landing screenshot in public/assets. Every
// page falls back to this for og:image / twitter:image so a shared link always
// renders a rich card, even pages that don't supply their own preview.
export const DEFAULT_OG_IMAGE = `${SITE_ORIGIN}/assets/embody-screenshot.png`;

// Promote a root-relative path (e.g. "/specimens/foo.jpg") to an absolute URL on
// the canonical origin. Already-absolute (http) inputs pass through untouched.
export function absoluteUrl(path: string): string {
  if (/^https?:\/\//.test(path)) return path;
  return `${SITE_ORIGIN}${path.startsWith("/") ? "" : "/"}${path}`;
}
