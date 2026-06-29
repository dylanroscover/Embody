// Transactional email via the Resend HTTP API (https://resend.com/docs/api-reference/emails/send-email).
//
// Safe-by-default: email is OPTIONAL. When RESEND_API_KEY is absent (local dev,
// CI build, or a prod deploy that hasn't wired email yet), sendEmail is a no-op
// that returns { sent: false, skipped: true } -- it NEVER throws and NEVER
// breaks the calling flow. Signup, password-reset requests, etc. all continue
// to work; the email is simply not delivered. The owner enables real delivery
// by setting RESEND_API_KEY (and optionally EMAIL_FROM) as Worker secrets/vars.

export interface EmailEnv {
  // Resend API key. When unset, all sends are skipped (no-op).
  RESEND_API_KEY?: string;
  // From address, e.g. "embody.tools <noreply@embody.tools>". Falls back to
  // DEFAULT_FROM when unset. Resend requires a verified sending domain.
  EMAIL_FROM?: string;
}

export interface SendEmailArgs {
  to: string;
  subject: string;
  html: string;
}

export interface SendEmailResult {
  // True only when Resend accepted the message (HTTP 2xx).
  sent: boolean;
  // True when the send was intentionally skipped because no API key is set.
  skipped?: boolean;
  // Populated when a real send attempt failed (key present, request rejected).
  error?: string;
}

// Default sender. Override with EMAIL_FROM once a domain is verified in Resend.
const DEFAULT_FROM = "embody.tools <noreply@embody.tools>";

const RESEND_ENDPOINT = "https://api.resend.com/emails";

interface ResendErrorBody {
  message?: string;
  name?: string;
}

// Send a single transactional email. Never throws -- failures are reported via
// the returned result so callers (and Better Auth's send hooks) can log without
// breaking the user-facing flow.
export async function sendEmail(env: EmailEnv, args: SendEmailArgs): Promise<SendEmailResult> {
  const apiKey = env.RESEND_API_KEY;
  if (!apiKey) {
    // No provider configured -- skip silently so dev/build/signup keep working.
    return { sent: false, skipped: true };
  }

  const from = env.EMAIL_FROM || DEFAULT_FROM;

  let response: Response;
  try {
    response = await fetch(RESEND_ENDPOINT, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        from,
        to: args.to,
        subject: args.subject,
        html: args.html
      }),
      // Bound the request. This fetch is awaited inline in user-facing flows
      // (signup verification, password reset, change-email); a hung Resend
      // endpoint would otherwise stall the response for the whole Worker budget.
      signal: AbortSignal.timeout(8000)
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error("sendEmail: network error", message);
    return { sent: false, error: message };
  }

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as ResendErrorBody;
      if (body?.message) detail = body.message;
    } catch {
      // Non-JSON error body -- keep the status-code detail.
    }
    console.error("sendEmail: Resend rejected the message", detail);
    return { sent: false, error: detail };
  }

  return { sent: true };
}

// True when a real email provider is configured. Drives safe-by-default gating
// (e.g. requireEmailVerification only when we can actually send the email).
export function emailEnabled(env: EmailEnv): boolean {
  return Boolean(env.RESEND_API_KEY);
}
