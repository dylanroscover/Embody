import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { requireUser } from "../../../../server/auth";
import { notifyOwnerNewReport } from "../../../../server/notifications";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";
import {
  createReport,
  getSpecimenIdBySlug,
  isReportReason,
  REPORT_REASONS
} from "../../../../server/engagement";

export const prerender = false;

// POST /api/specimens/:slug/report
// Files a moderation report against a specimen. Requires a signed-in user (401
// otherwise). The body is { reason } validated against the bounded REPORT_REASONS
// vocabulary (400 on anything else). Resolves the specimen id from the slug,
// appends the report, and returns 201 { id, reason, status }.
export const POST: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    let user;
    try {
      user = await requireUser(request, env);
    } catch {
      return errorResponse(401, "authentication_required", "A signed-in user is required to report.");
    }

    const reason = await readReason(request);
    if (!reason) {
      return errorResponse(
        400,
        "invalid_reason",
        `reason is required and must be one of: ${REPORT_REASONS.join(", ")}.`
      );
    }

    const specimenId = await getSpecimenIdBySlug(env.DB, slug);
    if (!specimenId) {
      return errorResponse(404, "specimen_not_found", "No specimen exists for that slug.");
    }

    const result = await createReport(env.DB, specimenId, user.id, reason);

    // Moderation signal to the owner. Self-swallowing + safe-by-default
    // (notifications.ts), so a notification failure never affects the 201.
    await notifyOwnerNewReport(env as CloudflareEnv, { slug, reason, reporterHandle: user.handle });

    return jsonResponse(result, {
      status: 201,
      headers: {
        Location: `/api/specimens/${slug}/report`
      }
    });
  } catch (error) {
    console.error("POST /api/specimens/:slug/report failed", error);
    return serverErrorResponse();
  }
};

// Parse and validate the report reason from the JSON body. Returns the validated
// reason, or null when the body is unparseable or the reason is out of vocabulary.
async function readReason(request: Request): Promise<import("../../../../server/engagement").ReportReason | null> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return null;
  }

  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }

  const reason = (raw as Record<string, unknown>).reason;
  return isReportReason(reason) ? reason : null;
}
