import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getSpecimenBySlug } from "../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";

export const prerender = false;

export const GET: APIRoute = async ({ params }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    const specimen = await getSpecimenBySlug(env.DB, slug);
    if (!specimen) {
      return errorResponse(404, "specimen_not_found", "No public specimen exists for that slug.");
    }

    return jsonResponse(specimen, {
      headers: {
        "Cache-Control": "public, max-age=60, s-maxage=300"
      }
    });
  } catch {
    return serverErrorResponse();
  }
};
