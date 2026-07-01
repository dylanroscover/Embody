import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getAuth, type AuthEnv } from "../../../lib/auth";
import { checkRateLimit, rateLimitDisabled, type RateLimitOptions } from "../../../server/rateLimit";
import { errorResponse } from "../../../server/http";

// Better Auth catch-all handler. Mounts every Better Auth endpoint under
// /api/auth/* (sign-up/email, sign-in/email, sign-out, get-session, the GitHub
// OAuth callback when wired, etc.). The auth instance is built lazily here so
// the D1 binding (env.DB) is read inside the request context.
export const prerender = false;

// Per-IP rate limits on the abuse-prone Better Auth POST endpoints. The catch-all
// otherwise forwards everything to auth.handler with no throttle, leaving sign-in
// open to credential stuffing/brute force, sign-up to bulk account creation, and
// the email senders (password reset / verification) to mailbombing an arbitrary
// address. KV-backed and fail-open in dev (no KV) -- see server/rateLimit.ts.
const AUTH_RATE_LIMITS: Array<{ test: RegExp; tag: string; limit: RateLimitOptions }> = [
  { test: /\/sign-in\/email\/?$/, tag: "signin", limit: { limit: 10, windowSec: 300 } },
  { test: /\/sign-up\/email\/?$/, tag: "signup", limit: { limit: 6, windowSec: 600 } },
  {
    // The two real password-reset POSTs this app mounts (emailAndPassword):
    // authClient.requestPasswordReset -> /request-password-reset, and
    // authClient.resetPassword -> /reset-password.
    test: /\/(request-password-reset|reset-password)\/?$/,
    tag: "pwreset",
    limit: { limit: 5, windowSec: 600 }
  },
  { test: /\/send-verification-email\/?$/, tag: "verify", limit: { limit: 5, windowSec: 600 } }
];

export const ALL: APIRoute = async (ctx) => {
  const { request } = ctx;
  // Only POSTs mutate / send mail; GETs (get-session, OAuth callback) are exempt.
  // Skip entirely in development (rateLimitDisabled) so the e2e suite -- which
  // bursts many sign-ups from one origin -- never throttles itself; prod limits
  // by the real CF-Connecting-IP.
  if (request.method === "POST" && !rateLimitDisabled(env)) {
    const rule = AUTH_RATE_LIMITS.find((r) => r.test.test(new URL(request.url).pathname));
    if (rule) {
      const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
      const rate = await checkRateLimit(env.KV, `auth:${rule.tag}:${ip}`, rule.limit);
      if (!rate.ok) {
        return errorResponse(
          429,
          "rate_limited",
          "Too many attempts. Please slow down and try again shortly.",
          rate.retryAfter ? { "Retry-After": String(rate.retryAfter) } : undefined
        );
      }
    }
  }

  const auth = getAuth(env as unknown as AuthEnv);
  return auth.handler(request);
};
