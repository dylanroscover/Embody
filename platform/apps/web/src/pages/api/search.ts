import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { searchSpecimensFts } from "../../server/db";
import { jsonResponse, serverErrorResponse } from "../../server/http";

export const prerender = false;

export const GET: APIRoute = async ({ url }) => {
  try {
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
  } catch {
    return serverErrorResponse();
  }
};

function parseLimit(value: string | null): number {
  if (!value) return 24;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : 24;
}
