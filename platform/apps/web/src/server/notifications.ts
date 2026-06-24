// Owner-facing operational notifications: the platform tells its operator when
// something worth knowing happens -- a new signup, a new specimen published to
// the public gallery, or an abuse report filed against content.
//
// Built on the SAME safe-by-default Resend layer as user email (src/server/email.ts):
// every send no-ops when RESEND_API_KEY is unset and NEVER throws. On top of that,
// the helpers here swallow their own errors and resolve to a result object, so a
// missing or failing owner-notification can never break the signup, publish, or
// report flow it rides on. Call sites can `await` them without a try/catch.
//
// Delivery requires RESEND_API_KEY (Worker secret); the destination is
// OWNER_NOTIFY_EMAIL (defaults to the project owner). See the README env table.

import { sendEmail, type EmailEnv, type SendEmailResult } from "./email";
import { detailRows, renderEmail } from "./emailTemplate";

export interface NotifyEnv extends EmailEnv {
  // Owner inbox for operational notifications. Falls back to DEFAULT_OWNER.
  OWNER_NOTIFY_EMAIL?: string;
  // Public base URL, used to build absolute links in the notifications. Falls
  // back to DEFAULT_BASE_URL when unset (same value used for OAuth callbacks).
  BETTER_AUTH_URL?: string;
}

// Public site origin, used only to build links inside notification bodies.
const DEFAULT_BASE_URL = "https://embody.tools";

// Where owner notifications go. Sourced ONLY from OWNER_NOTIFY_EMAIL -- no email
// is hardcoded. Unset means owner notices are skipped (see notifyOwner).
function ownerAddress(env: NotifyEnv): string {
  return env.OWNER_NOTIFY_EMAIL?.trim() || "";
}

function baseUrl(env: NotifyEnv): string {
  return (env.BETTER_AUTH_URL?.trim() || DEFAULT_BASE_URL).replace(/\/+$/, "");
}

// Send one owner notice. Defensive: never throws, returns the send result (or a
// skipped/error result) so a notification failure cannot break the caller. The
// HTML is the SHARED branded shell (renderEmail) used by the user emails too, so
// the owner notices carry the same logo/green theme.
async function notifyOwner(
  env: NotifyEnv,
  subject: string,
  heading: string,
  rows: Array<[label: string, value: string]>,
  link?: { href: string; label: string }
): Promise<SendEmailResult> {
  try {
    const to = ownerAddress(env);
    // No owner inbox configured (OWNER_NOTIFY_EMAIL unset) -> skip, never error.
    if (!to) return { sent: false, skipped: true };
    return await sendEmail(env, {
      to,
      subject,
      html: renderEmail({
        heading,
        bodyHtml: detailRows(rows),
        cta: link ? { href: link.href, label: link.label } : undefined,
        footerNote:
          "embody.tools operational notification. Set OWNER_NOTIFY_EMAIL to change where these go."
      })
    });
  } catch (error) {
    // sendEmail already never throws; this guards the HTML build / address
    // resolution so the calling flow (signup/publish/report) is never broken.
    const message = error instanceof Error ? error.message : String(error);
    console.error("notifyOwner failed", subject, message);
    return { sent: false, error: message };
  }
}

// A new account was created (email+password or GitHub). Fired from the Better
// Auth user.create.after hook; handle is the freshly-derived profile handle.
export async function notifyOwnerNewSignup(
  env: NotifyEnv,
  args: { email: string | null; handle: string | null }
): Promise<SendEmailResult> {
  const handle = args.handle ?? "(pending)";
  const rows: Array<[string, string]> = [
    ["Email", args.email || "(none)"],
    ["Handle", handle]
  ];
  const link = args.handle
    ? { href: `${baseUrl(env)}/u/${encodeURIComponent(args.handle)}`, label: "View profile" }
    : undefined;
  return notifyOwner(env, "New embody.tools signup", "New signup", rows, link);
}

// A specimen was published to the public gallery. Fired after the D1 insert in
// the submit endpoint. scanVerdict surfaces flagged-but-accepted content (worth
// an owner glance); blocked/malware submissions are rejected before this point.
export async function notifyOwnerNewSpecimen(
  env: NotifyEnv,
  args: { title: string; slug: string; handle: string; scanVerdict?: string }
): Promise<SendEmailResult> {
  const rows: Array<[string, string]> = [
    ["Title", args.title],
    ["By", args.handle],
    ["Scan", args.scanVerdict || "allowed"]
  ];
  const link = { href: `${baseUrl(env)}/c/${encodeURIComponent(args.slug)}`, label: "View specimen" };
  return notifyOwner(env, `New specimen: ${args.title}`, "New specimen published", rows, link);
}

// An abuse report was filed against a specimen. Fired after the report row is
// written. This is the moderation signal -- it links straight to the content.
export async function notifyOwnerNewReport(
  env: NotifyEnv,
  args: { slug: string; reason: string; reporterHandle: string }
): Promise<SendEmailResult> {
  const rows: Array<[string, string]> = [
    ["Specimen", args.slug],
    ["Reason", args.reason],
    ["Reported by", args.reporterHandle]
  ];
  const link = { href: `${baseUrl(env)}/c/${encodeURIComponent(args.slug)}`, label: "Review specimen" };
  return notifyOwner(env, `Specimen reported: ${args.slug}`, "Specimen reported", rows, link);
}
