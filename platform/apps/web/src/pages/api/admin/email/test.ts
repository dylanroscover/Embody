import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { assertSameOrigin, isEmailAddress, isEmailTemplate, requireAdmin } from "../../../../server/admin";
import { renderSampleEmail } from "../../../../server/emailTemplate";
import { sendEmail } from "../../../../server/email";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";

export const prerender = false;

// Admin-only: test-send one of the 5 branded email templates so the owner can
// confirm rendering across clients. Returns the RAW send result -- { sent } on
// success, { skipped: true } when no RESEND_API_KEY is configured (CI/dev), or
// { error } on a real send failure -- so the console shows the true outcome.
export const POST: APIRoute = async ({ request }) => {
  try {
    let admin;
    try {
      admin = await requireAdmin(request, env);
    } catch (res) {
      return res as Response;
    }
    const csrf = assertSameOrigin(request);
    if (csrf) return csrf;

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_body", "A JSON body with a template is required.");
    }
    const raw = (body ?? {}) as { template?: unknown; to?: unknown };

    if (!isEmailTemplate(raw.template)) {
      return errorResponse(
        400,
        "invalid_template",
        "template must be one of: verification, reset-password, new-signup, new-specimen, new-report."
      );
    }

    // Recipient defaults to the admin's own email. An explicit address is allowed
    // (send to a personal Gmail/Outlook/Apple Mail to check cross-client), but
    // validated. Admin-gated, so this is not an open relay.
    let to = admin.email;
    if (raw.to !== undefined && String(raw.to).trim() !== "") {
      if (!isEmailAddress(raw.to)) {
        return errorResponse(400, "invalid_recipient", "to must be a valid email address.");
      }
      to = String(raw.to).trim();
    }
    if (!to) return errorResponse(400, "invalid_recipient", "No recipient address available.");

    const sample = renderSampleEmail(raw.template);
    const result = await sendEmail(env as unknown as CloudflareEnv, {
      to,
      subject: `[test] ${sample.subject}`,
      html: sample.html
    });

    console.log("ADMIN action", {
      actor: admin.id,
      action: "email.test",
      target: to,
      value: raw.template
    });
    return jsonResponse({ to, template: raw.template, ...result });
  } catch (error) {
    console.error("POST /api/admin/email/test failed", error);
    return serverErrorResponse();
  }
};
