import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { searchSpecimensFts } from "../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../server/http";
import { checkRateLimit, rateLimitDisabled } from "../../server/rateLimit";

export const prerender = false;

// Per-IP cap. The response is CDN-cached, but an attacker can vary ?q= to defeat
// the cache and drive D1 FTS load directly, so bound it before the query runs.
const SEARCH_RATE_LIMIT = { limit: 60, windowSec: 60 };

export const GET: APIRoute = async ({ url, request }) => {
  try {
    // Skip in development (e2e); limit by real IP in production.
    if (!rateLimitDisabled(env)) {
      const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
      const rate = await checkRateLimit(env.KV, `search:${ip}`, SEARCH_RATE_LIMIT);
      if (!rate.ok) {
        return errorResponse(
          429,
          "rate_limited",
          "Too many searches. Please slow down and try again shortly.",
          rate.retryAfter ? { "Retry-After": String(rate.retryAfter) } : undefined
        );
      }
    }

    const response = await searchSpecimensFts(
      env.DB,
      url.searchParams.get("q") ?? "",
      parseLimit(url.searchParams.get("limit"))
    );

    return jsonResponse(response, {
      headers: {
        "Cache-Control": "public, max-age=30, s-maxage=120"
      }
    });
  } catch (error) {
    // Was a bare `catch {}` -- production search failures were invisible.
    console.error("GET /api/search failed", error);
    return serverErrorResponse();
  }
};

function parseLimit(value: string | null): number {
  if (!value) return 24;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : 24;
}
