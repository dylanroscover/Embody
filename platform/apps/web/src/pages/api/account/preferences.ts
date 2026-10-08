import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getRequestUser } from "../../../server/auth";
import { RESULT_PANEL_SCALE_MAX, RESULT_PANEL_SCALE_MIN, setResultPanelScale } from "../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";

export const prerender = false;

// POST /api/account/preferences -- the signed-in user's UI preferences:
// { resultPanelScale: number | null }, the specimen page's rendered-result panel
// size as a multiple of its default (null resets it). Out-of-range values are
// clamped, not rejected, so a stale client never loses the write.
export const POST: APIRoute = async ({ request }) => {
  try {
    const user = await getRequestUser(request, env);
    if (!user) return errorResponse(401, "unauthorized", "Sign in to save preferences.");

    let body: { resultPanelScale?: unknown };
    try {
      body = (await request.json()) as { resultPanelScale?: unknown };
    } catch {
      return errorResponse(400, "invalid_body", "Expected a JSON body.");
    }

    const raw = body.resultPanelScale;
    if (raw !== null && (typeof raw !== "number" || !Number.isFinite(raw))) {
      return errorResponse(400, "invalid_preferences", "resultPanelScale must be a number or null.");
    }
    const scale = raw === null ? null : Math.min(RESULT_PANEL_SCALE_MAX, Math.max(RESULT_PANEL_SCALE_MIN, raw));
    await setResultPanelScale(env.DB, user.id, scale);
    return jsonResponse({ updated: true, resultPanelScale: scale });
  } catch {
    return serverErrorResponse();
  }
};
