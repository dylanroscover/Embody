import { betterAuth } from "better-auth";
import { D1Dialect } from "kysely-d1";
import { emailEnabled, sendEmail } from "../server/email";
import { notifyOwnerNewSignup } from "../server/notifications";

// Better Auth server config for the Cloudflare Worker (Astro server output).
//
// IMPORTANT (Workers + D1): a D1 binding only exists INSIDE a request context.
// Constructing the auth instance (which builds the Kysely dialect over env.DB)
// at module top-level fails in Workers, so we build it lazily per request and
// memoize per-binding. Every route/middleware calls getAuth(env) inside the
// handler, where env.DB is live.
//
// D1 has no interactive transactions; Better Auth's Kysely adapter falls back
// to D1's batch() API, so no extra transaction config is required here.

export interface AuthEnv {
  DB: D1Database;
  BETTER_AUTH_SECRET?: string;
  BETTER_AUTH_URL?: string;
  GITHUB_CLIENT_ID?: string;
  GITHUB_CLIENT_SECRET?: string;
  ENVIRONMENT?: string;
  // Transactional email (Resend). Optional -- when unset, email verification
  // and password-reset emails are skipped and verification is NOT required, so
  // signup keeps working without an email provider. See src/server/email.ts.
  RESEND_API_KEY?: string;
  EMAIL_FROM?: string;
  // Owner inbox for operational notifications (new signup). Optional; defaults
  // to the project owner. See src/server/notifications.ts.
  OWNER_NOTIFY_EMAIL?: string;
}

// True when GitHub OAuth has been wired by the owner (see owner_todo).
export function githubOAuthEnabled(env: AuthEnv): boolean {
  return Boolean(env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET);
}

function buildAuth(env: AuthEnv, secret: string) {
  const socialProviders = githubOAuthEnabled(env)
    ? {
        github: {
          clientId: env.GITHUB_CLIENT_ID as string,
          clientSecret: env.GITHUB_CLIENT_SECRET as string
        }
      }
    : undefined;

  // Safe-by-default: only require email verification (and thus block sign-in
  // until verified) when a real email provider is configured. Without one, the
  // verification email could never be delivered, so signup must stay usable
  // immediately. See src/server/email.ts.
  const hasEmail = emailEnabled(env);

  return betterAuth({
    secret,
    // baseURL is optional; when unset Better Auth derives it from the request.
    // Set BETTER_AUTH_URL in production so OAuth callbacks resolve correctly.
    ...(env.BETTER_AUTH_URL ? { baseURL: env.BETTER_AUTH_URL } : {}),
    database: {
      dialect: new D1Dialect({ database: env.DB }),
      type: "sqlite"
    },
    emailAndPassword: {
      enabled: true,
      // Verification is required only when email is configured; otherwise the
      // account is usable immediately (no provider to deliver the email).
      requireEmailVerification: hasEmail,
      autoSignIn: true,
      // Password reset: emails the user a tokenized link to reset-password.
      // No-op when RESEND_API_KEY is absent (sendEmail skips), so the
      // /request-password-reset endpoint still responds without breaking.
      sendResetPassword: async (data) => {
        await sendEmail(env, {
          to: data.user.email,
          subject: "Reset your embody.tools password",
          html: resetPasswordEmailHtml(data.url)
        });
      }
    },
    // Email verification: sends the verification link on sign up. The hook is
    // always wired; sendEmail itself no-ops when no provider is configured, so
    // an unconfigured deploy never errors out of the signup path.
    emailVerification: {
      sendOnSignUp: hasEmail,
      autoSignInAfterVerification: true,
      sendVerificationEmail: async (data) => {
        await sendEmail(env, {
          to: data.user.email,
          subject: "Verify your embody.tools email",
          html: verificationEmailHtml(data.url)
        });
      }
    },
    ...(socialProviders ? { socialProviders } : {}),
    // Link the Better Auth user to our app-domain users_profile row on creation.
    // Direct D1 query (we intentionally do NOT touch src/server/db.ts).
    databaseHooks: {
      user: {
        create: {
          after: async (user: { id: string; email?: string | null; name?: string | null }) => {
            const handle = await ensureUserProfile(env.DB, {
              id: user.id,
              email: user.email,
              name: user.name
            });
            // Operational notice to the owner. Safe-by-default + self-swallowing,
            // so it never blocks or breaks account creation (see notifications.ts).
            await notifyOwnerNewSignup(env, { email: user.email ?? null, handle });
          }
        }
      }
    }
  });
}

type AuthInstance = ReturnType<typeof buildAuth>;

// Memoize per D1 binding object. In a warm Worker isolate the same binding
// instance is reused across requests, so this avoids rebuilding the adapter
// every request while staying correct if the isolate is recycled.
const cache = new WeakMap<D1Database, AuthInstance>();

export function getAuth(env: AuthEnv): AuthInstance {
  const existing = cache.get(env.DB);
  if (existing) return existing;

  const secret = env.BETTER_AUTH_SECRET;
  if (!secret) {
    // Fail loud: a missing secret would silently weaken every session token.
    throw new Error(
      "BETTER_AUTH_SECRET is not set. Add it to wrangler vars (or a Worker secret) before using auth."
    );
  }

  const instance = buildAuth(env, secret);
  cache.set(env.DB, instance);
  return instance;
}

// Create the 1:1 users_profile row for a freshly created Better Auth user.
// trust_level 'verified' (an authenticated, email+password or GitHub user is a
// real account, distinct from the 'anon' default). Handle is derived and made
// unique against existing handles. Best-effort: never throw out of the signup
// path -- a failure here must not block account creation, and the row is also
// backfilled lazily by ensureProfileForSession() on first authed request.
export async function ensureUserProfile(
  db: D1Database,
  user: { id: string; email?: string | null; name?: string | null }
): Promise<string | null> {
  try {
    const existing = await db
      .prepare("SELECT handle FROM users_profile WHERE id = ? LIMIT 1")
      .bind(user.id)
      .first<{ handle: string }>();
    if (existing?.handle) return existing.handle;

    const handle = await uniqueHandle(db, deriveHandleBase(user.email, user.name));

    await db
      .prepare(
        `INSERT OR IGNORE INTO users_profile (id, handle, trust_level)
         VALUES (?, ?, 'verified')`
      )
      .bind(user.id, handle)
      .run();

    // If the INSERT was ignored due to a race, read back the live handle.
    const row = await db
      .prepare("SELECT handle FROM users_profile WHERE id = ? LIMIT 1")
      .bind(user.id)
      .first<{ handle: string }>();
    return row?.handle ?? handle;
  } catch (error) {
    console.error("ensureUserProfile failed", error);
    return null;
  }
}

function deriveHandleBase(email?: string | null, name?: string | null): string {
  const fromEmail = (email ?? "").split("@")[0] ?? "";
  const raw = (name && name.trim()) || fromEmail || "user";
  const slug = raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32);
  return slug || "user";
}

async function uniqueHandle(db: D1Database, base: string): Promise<string> {
  let candidate = base;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const existing = await db
      .prepare("SELECT 1 AS hit FROM users_profile WHERE handle = ? LIMIT 1")
      .bind(candidate)
      .first<{ hit: number }>();
    if (!existing) return candidate;
    candidate = `${base}-${randomSuffix()}`;
  }
  return `${base}-${crypto.randomUUID().slice(0, 8)}`;
}

function randomSuffix(): string {
  const bytes = new Uint8Array(3);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Minimal escaping for the one untrusted value (the URL) interpolated into the
// transactional email bodies below. The URL is generated by Better Auth, but we
// escape defensively so a crafted value can never break out of the attribute.
function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function emailShell(heading: string, intro: string, url: string, ctaLabel: string): string {
  const safeUrl = escapeHtml(url);
  return `<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#0e0e12;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#e8e8ec;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:480px;margin:0 auto;">
      <tr><td>
        <h1 style="font-size:20px;margin:0 0 16px;color:#ffffff;">${heading}</h1>
        <p style="font-size:15px;line-height:1.5;margin:0 0 24px;color:#c7c7cf;">${intro}</p>
        <p style="margin:0 0 24px;">
          <a href="${safeUrl}" style="display:inline-block;padding:12px 20px;background:#6c5ce7;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;font-size:15px;">${ctaLabel}</a>
        </p>
        <p style="font-size:13px;line-height:1.5;margin:0 0 8px;color:#8a8a93;">Or paste this link into your browser:</p>
        <p style="font-size:13px;line-height:1.5;margin:0 0 24px;word-break:break-all;"><a href="${safeUrl}" style="color:#9b8cff;">${safeUrl}</a></p>
        <p style="font-size:12px;line-height:1.5;margin:0;color:#6a6a73;">If you did not request this, you can safely ignore this email.</p>
      </td></tr>
    </table>
  </body>
</html>`;
}

function verificationEmailHtml(url: string): string {
  return emailShell(
    "Verify your email",
    "Confirm your email address to finish setting up your embody.tools account.",
    url,
    "Verify email"
  );
}

function resetPasswordEmailHtml(url: string): string {
  return emailShell(
    "Reset your password",
    "We received a request to reset the password for your embody.tools account. This link expires in one hour.",
    url,
    "Reset password"
  );
}
