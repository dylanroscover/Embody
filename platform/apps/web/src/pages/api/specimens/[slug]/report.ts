import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { requireUser } from "../../../../server/auth";
import { clientIp } from "../../../../server/admin";
import { checkRateLimit, rateLimitDisabled } from "../../../../server/rateLimit";
import { notifyOwnerNewReport } from "../../../../server/notifications";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";
import {
  countDistinctReportersForAuthor,
  countDistinctReportersForSpecimen,
  getSpecimenAuthorId,
  logEvent,
  setSpecimenVisibility,
  setUserBanned
} from "../../../../server/db";
import {
  createReport,
  getSpecimenIdBySlug,
  isReportReason,
  REPORT_REASONS
} from "../../../../server/engagement";

export const prerender = false;

// Auto-moderation thresholds, counted by DISTINCT reporters (so one person can't
// brigade an account into a takedown). At AUTO_HIDE the reported specimen is
// pulled private pending review; at AUTO_BAN the author is suspended outright.
const AUTO_HIDE_THRESHOLD = 3;
const AUTO_BAN_THRESHOLD = 5;

// Per-user cap. Each report writes a row, an audit event, and fires an owner
// email; without a bound one account can flood the moderation queue and mailbox.
const REPORT_RATE_LIMIT = { limit: 10, windowSec: 300 };

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

    if (!rateLimitDisabled(env)) {
      const rate = await checkRateLimit(env.KV, `report:${user.id}`, REPORT_RATE_LIMIT);
      if (!rate.ok) {
        return errorResponse(
          429,
          "rate_limited",
          "Too many reports. Please slow down and try again shortly.",
          rate.retryAfter ? { "Retry-After": String(rate.retryAfter) } : undefined
        );
      }
    }

    const parsed = await readReportBody(request);
    if (!parsed) {
      return errorResponse(
        400,
        "invalid_reason",
        `reason is required and must be one of: ${REPORT_REASONS.join(", ")}.`
      );
    }
    const { reason, details } = parsed;
    if (reason === "other" && !details) {
      return errorResponse(
        400,
        "details_required",
        "Please add a short note describing the issue for an 'other' report."
      );
    }

    const specimenId = await getSpecimenIdBySlug(env.DB, slug);
    if (!specimenId) {
      return errorResponse(404, "specimen_not_found", "No specimen exists for that slug.");
    }

    const result = await createReport(env.DB, specimenId, user.id, reason, details);

    await logEvent(env.DB, {
      actorId: user.id,
      actorHandle: user.handle,
      action: "report.create",
      targetType: "specimen",
      targetId: specimenId,
      metadata: { slug, reason },
      ip: clientIp(request)
    });

    // Distinct-reporter auto-moderation. Best-effort and fully guarded -- any
    // failure here is swallowed so it never affects the 201 the reporter gets.
    await maybeAutoModerate(slug, specimenId, clientIp(request));

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

// Distinct-reporter auto-moderation. AUTO_HIDE pulls the reported specimen
// private; AUTO_BAN suspends its author. Privileged authors (curator/admin) and
// already-banned authors are never auto-acted on. Wrapped so any failure is
// swallowed -- auto-moderation must never break the report's 201.
async function maybeAutoModerate(
  slug: string,
  specimenId: string,
  ip: string | null
): Promise<void> {
  try {
    // Auto-hide the specimen once enough distinct people have flagged it.
    const specimenReporters = await countDistinctReportersForSpecimen(env.DB, specimenId);
    if (specimenReporters >= AUTO_HIDE_THRESHOLD) {
      const hidden = await setSpecimenVisibility(env.DB, specimenId, "private");
      if (hidden) {
        await logEvent(env.DB, {
          action: "specimen.auto_hide",
          targetType: "specimen",
          targetId: specimenId,
          metadata: { slug, distinctReporters: specimenReporters, threshold: AUTO_HIDE_THRESHOLD },
          ip
        });
      }
    }

    // Auto-ban the author once enough distinct people have flagged their content.
    const authorId = await getSpecimenAuthorId(env.DB, specimenId);
    if (!authorId) return;

    const author = await env.DB.prepare(
      "SELECT trust_level, banned FROM users_profile WHERE id = ? LIMIT 1"
    )
      .bind(authorId)
      .first<{ trust_level: string; banned: number }>();
    // Never auto-ban a privileged or already-banned account.
    if (!author || author.banned || author.trust_level === "admin" || author.trust_level === "curator") {
      return;
    }

    const authorReporters = await countDistinctReportersForAuthor(env.DB, authorId);
    if (authorReporters >= AUTO_BAN_THRESHOLD) {
      const banned = await setUserBanned(
        env.DB,
        authorId,
        true,
        `Auto-banned: ${authorReporters} distinct reporters flagged this account's content.`
      );
      if (banned) {
        await logEvent(env.DB, {
          action: "user.auto_ban",
          targetType: "user",
          targetId: authorId,
          metadata: { distinctReporters: authorReporters, threshold: AUTO_BAN_THRESHOLD },
          ip
        });
      }
    }
  } catch (error) {
    console.error("maybeAutoModerate failed", error);
  }
}

const MAX_DETAILS = 2000;

// Parse + validate the JSON body. Returns the validated reason plus an optional
// free-text note (trimmed, length-capped; null when blank/absent), or null when
// the body is unparseable or the reason is out of vocabulary. The "other"-needs-a
// -note rule is enforced by the caller.
async function readReportBody(
  request: Request
): Promise<{ reason: import("../../../../server/engagement").ReportReason; details: string | null } | null> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return null;
  }

  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }

  const obj = raw as Record<string, unknown>;
  if (!isReportReason(obj.reason)) {
    return null;
  }

  let details: string | null = null;
  if (typeof obj.details === "string") {
    const trimmed = obj.details.trim().slice(0, MAX_DETAILS);
    details = trimmed.length > 0 ? trimmed : null;
  }

  return { reason: obj.reason, details };
}
