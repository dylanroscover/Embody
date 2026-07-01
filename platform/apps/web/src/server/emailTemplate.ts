// One branded shell for every outgoing email -- the SINGLE source of truth used
// by both src/lib/auth.ts (verification + password-reset) and
// src/server/notifications.ts (owner notices). Before this module each of those
// files carried its own escapeHtml + its own generic purple/system-font shell;
// they now both render through renderEmail() so the brand can never drift.
//
// Design constraints (email is not the web):
// - INLINE styles only. CSS custom properties (var(--accent)) do NOT work in
//   email clients, so every color is an inline hex literal taken from the site
//   palette in src/styles/embody.css (:root). Keep them in sync by hand.
// - Table-based layout, no fl/ex/grid, no SVG. Survives Outlook (Word engine).
// - Brand survives image-blocking: the logo is a hosted PNG, but a live-text
//   "embody.tools" wordmark sits beside it, so a blocked image still reads as
//   the brand. The CTA is a <td bgcolor> + padded <a> (renders as a real button
//   even in Outlook, which ignores border-radius -> square but correct).
// - Web fonts (Inter) are offered via a <link> for clients that honor it, but
//   every inline font-family carries a full system fallback, so font-stripping
//   clients render cleanly. The mono footer falls back to the system mono stack.

// Brand palette (mirrors src/styles/embody.css :root). Email needs literals.
const BG = "#181e1e"; // --bg (outer)
const CARD = "#1f2321"; // --bg-elevated (the email card)
const BORDER = "#2a4a42"; // --border
const HEADING = "#eaf2ea"; // slightly brighter than --text for headings
const TEXT = "#c8d0c9"; // --text
const MUTED = "#97a098"; // --text-muted
const FAINT = "#6b756c"; // --text-faint
const ACCENT = "#6ee668"; // --accent (CTA fill, links)
const ON_ACCENT = "#181e1e"; // dark label on the green button (contrast)

// Hosted logo mark. MUST be an absolute, production URL -- a request-derived or
// localhost-relative src would 404 in a real inbox. Served from public/email/.
const BRAND_ORIGIN = "https://embody.tools";
const LOGO_URL = `${BRAND_ORIGIN}/email/embody-mark.png`;

const FONT_SANS = "Inter,system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif";
const FONT_MONO = "'JetBrains Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace";

const DEFAULT_FOOTER = "embody.tools - transparent TouchDesigner networks";

// Minimal HTML escaping for every untrusted value interpolated into an email
// (URLs, emails, handles, titles, reasons). The one escaper for all email code.
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export interface EmailCta {
  href: string;
  label: string;
}

export interface RenderEmailArgs {
  heading: string;
  // Trusted inner HTML for the body. Callers that interpolate untrusted values
  // (detailRows, the owner notices) escape them; the static user-email intros
  // contain no untrusted data.
  bodyHtml: string;
  cta?: EmailCta;
  // "Or paste this link" fallback row under the CTA (escaped here).
  fallbackUrl?: string;
  // Small-print footer line. Defaults to the brand tagline; owner emails pass
  // their operational-notice line.
  footerNote?: string;
}

// Branded green CTA button. The padding lives on the <td> (with the green
// bgcolor), NOT on the <a> -- Outlook's Word engine strips padding from anchors,
// which would collapse an anchor-padded button to a sliver. With the padding on
// the cell, Outlook renders a real padded green button (square corners, since it
// ignores border-radius); modern clients get the rounded button. The <a> is the
// link text inside it.
function ctaButton(cta: EmailCta): string {
  const href = escapeHtml(cta.href);
  const label = escapeHtml(cta.label);
  return `<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px;">
        <tr>
          <td align="center" bgcolor="${ACCENT}" style="border-radius:8px;padding:12px 22px;">
            <a href="${href}" target="_blank" rel="noopener" style="display:inline-block;font-family:${FONT_SANS};font-size:15px;font-weight:600;line-height:1;color:${ON_ACCENT};text-decoration:none;">${label}</a>
          </td>
        </tr>
      </table>`;
}

// The full branded HTML document. heading + bodyHtml + optional CTA + optional
// paste-link fallback + footer, inside a dark card with the embody header.
export function renderEmail(args: RenderEmailArgs): string {
  const { heading, bodyHtml, cta, fallbackUrl, footerNote } = args;
  const safeHeading = escapeHtml(heading);
  const footer = escapeHtml(footerNote ?? DEFAULT_FOOTER);

  const ctaHtml = cta ? ctaButton(cta) : "";

  const fallbackHtml = fallbackUrl
    ? `<p style="font-size:13px;line-height:1.5;margin:0 0 8px;color:${MUTED};font-family:${FONT_SANS};">Or paste this link into your browser:</p>
        <p style="font-size:13px;line-height:1.5;margin:0 0 24px;word-break:break-all;font-family:${FONT_MONO};"><a href="${escapeHtml(fallbackUrl)}" style="color:${ACCENT};">${escapeHtml(fallbackUrl)}</a></p>`
    : "";

  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="dark" />
    <meta name="supported-color-schemes" content="dark" />
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet" />
    <title>${safeHeading}</title>
  </head>
  <body style="margin:0;padding:0;background:${BG};">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:${BG};">
      <tr>
        <td align="center" style="padding:32px 16px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:520px;background:${CARD};border:1px solid ${BORDER};border-radius:14px;">
            <tr>
              <td style="padding:24px 28px;border-bottom:1px solid ${BORDER};">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="padding-right:10px;vertical-align:middle;">
                      <img src="${LOGO_URL}" width="32" height="32" alt="embody.tools" style="display:block;border:0;border-radius:8px;" />
                    </td>
                    <td style="vertical-align:middle;font-family:${FONT_SANS};font-size:18px;font-weight:600;color:${TEXT};letter-spacing:-0.01em;">embody.tools</td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:28px;">
                <h1 style="font-family:${FONT_SANS};font-size:20px;font-weight:600;line-height:1.3;margin:0 0 16px;color:${HEADING};">${safeHeading}</h1>
                ${bodyHtml}
                ${ctaHtml}
                ${fallbackHtml}
              </td>
            </tr>
            <tr>
              <td style="padding:18px 28px;border-top:1px solid ${BORDER};">
                <p style="font-family:${FONT_MONO};font-size:12px;line-height:1.5;margin:0;color:${FAINT};">${footer}</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>`;
}

// A simple branded paragraph for user-email intros (Inter, --text).
export function bodyParagraph(text: string): string {
  return `<p style="font-family:${FONT_SANS};font-size:15px;line-height:1.6;margin:0 0 24px;color:${TEXT};">${escapeHtml(text)}</p>`;
}

// Branded two-column label/value table for owner notices. Every label and value
// is escaped. Mirrors the old ownerNoticeHtml detail block, re-skinned.
export function detailRows(rows: Array<[label: string, value: string]>): string {
  const body = rows
    .map(
      ([label, value]) =>
        `<tr>
          <td style="padding:4px 12px 4px 0;color:${MUTED};font-family:${FONT_MONO};font-size:12px;white-space:nowrap;vertical-align:top;">${escapeHtml(label)}</td>
          <td style="padding:4px 0;color:${TEXT};font-family:${FONT_SANS};font-size:14px;word-break:break-word;">${escapeHtml(value)}</td>
        </tr>`
    )
    .join("");
  return `<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px;">${body}</table>`;
}

const OWNER_FOOTER =
  "embody.tools operational notification. Set OWNER_NOTIFY_EMAIL to change where these go.";

export interface SampleEmail {
  subject: string;
  html: string;
}

// Faithful sample renders of each of the 5 email types, for the admin email
// test-send console. They use the SAME shell + the same subjects/headings as the
// real emails in auth.ts / notifications.ts, with placeholder data and a sample
// link, so the owner confirms the true branded rendering across clients. The
// caller validates `template` against EMAIL_TEMPLATES first; default is a safe
// generic preview.
export function renderSampleEmail(template: string): SampleEmail {
  const verifyUrl =
    "https://embody.tools/api/auth/verify-email?token=SAMPLE_TOKEN&callbackURL=%2Fsignin%3Fverified%3D1";
  const resetUrl = "https://embody.tools/reset-password?token=SAMPLE_TOKEN";
  switch (template) {
    case "verification":
      return {
        subject: "Verify your embody.tools email",
        html: renderEmail({
          heading: "Verify your email",
          bodyHtml: bodyParagraph(
            "Confirm your email address to finish setting up your embody.tools account."
          ),
          cta: { href: verifyUrl, label: "Verify email" },
          fallbackUrl: verifyUrl,
          footerNote: "If you did not request this, you can safely ignore this email."
        })
      };
    case "reset-password":
      return {
        subject: "Reset your embody.tools password",
        html: renderEmail({
          heading: "Reset your password",
          bodyHtml: bodyParagraph(
            "We received a request to reset the password for your embody.tools account. This link expires in one hour."
          ),
          cta: { href: resetUrl, label: "Reset password" },
          fallbackUrl: resetUrl,
          footerNote: "If you did not request this, you can safely ignore this email."
        })
      };
    case "new-signup":
      return {
        subject: "New embody.tools signup",
        html: renderEmail({
          heading: "New signup",
          bodyHtml: detailRows([
            ["Email", "newuser@example.com"],
            ["Handle", "newuser"]
          ]),
          cta: { href: "https://embody.tools/u/newuser", label: "View profile" },
          footerNote: OWNER_FOOTER
        })
      };
    case "new-specimen":
      return {
        subject: "New specimen: Feedback Bloom",
        html: renderEmail({
          heading: "New specimen published",
          bodyHtml: detailRows([
            ["Title", "Feedback Bloom"],
            ["By", "newuser"],
            ["Scan", "allowed"]
          ]),
          cta: { href: "https://embody.tools/c/feedback-bloom", label: "View specimen" },
          footerNote: OWNER_FOOTER
        })
      };
    case "new-report":
      return {
        subject: "Specimen reported: feedback-bloom",
        html: renderEmail({
          heading: "Specimen reported",
          bodyHtml: detailRows([
            ["Specimen", "feedback-bloom"],
            ["Reason", "spam"],
            ["Reported by", "someuser"]
          ]),
          cta: { href: "https://embody.tools/c/feedback-bloom", label: "Review specimen" },
          footerNote: OWNER_FOOTER
        })
      };
    default:
      return {
        subject: "embody.tools email preview",
        html: renderEmail({
          heading: "Email preview",
          bodyHtml: bodyParagraph("This is a preview of the embody.tools email shell."),
          footerNote: DEFAULT_FOOTER
        })
      };
  }
}
