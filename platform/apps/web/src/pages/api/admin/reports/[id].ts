import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { assertSameOrigin, isReportStatus, requireAdmin } from "../../../../server/admin";
import { updateReportStatus } from "../../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";

export const prerender = false;

// Admin-only: move a moderation report through its status workflow
// (open -> reviewing -> actioned | dismissed). Non-admins get a 404 (the route
// is indistinguishable from a missing one).
export const POST: APIRoute = async ({ params, request }) => {
  try {
    let admin;
    try {
      admin = await requireAdmin(request, env);
    } catch (res) {
      return res as Response;
    }
    const csrf = assertSameOrigin(request);
    if (csrf) return csrf;

    const id = params.id;
    if (!id) return errorResponse(400, "invalid_id", "A report id is required.");

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_body", "A JSON body with a status is required.");
    }
    const status = (body as { status?: unknown } | null)?.status;
    if (!isReportStatus(status)) {
      return errorResponse(
        400,
        "invalid_status",
        "status must be one of: open, reviewing, actioned, dismissed."
      );
    }

    const ok = await updateReportStatus(env.DB, id, status);
    if (!ok) return errorResponse(404, "report_not_found", "No report exists for that id.");

    console.log("ADMIN action", {
      actor: admin.id,
      action: "report.status",
      target: id,
      value: status
    });
    return jsonResponse({ updated: true, id, status });
  } catch (error) {
    console.error("POST /api/admin/reports/[id] failed", error);
    return serverErrorResponse();
  }
};
