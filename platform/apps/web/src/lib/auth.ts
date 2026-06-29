import { betterAuth } from "better-auth";
import { D1Dialect } from "kysely-d1";
import { emailEnabled, sendEmail } from "../server/email";
import { notifyOwnerNewSignup } from "../server/notifications";
import { bodyParagraph, renderEmail } from "../server/emailTemplate";

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
    // Force the Secure flag + __Secure- cookie prefix in production. Better Auth
    // otherwise infers `secure` from NODE_ENV/baseURL, but NODE_ENV is unset on
    // the Worker and BETTER_AUTH_URL is intentionally unset (wrangler.jsonc), so
    // the inference falls through to false -> a session cookie with NO Secure
    // flag in production. Pin it to our own ENVIRONMENT var instead (which is
    // "development" only via .dev.vars for local http dev).
    advanced: { useSecureCookies: env.ENVIRONMENT === "production" },
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
    // Owner email change. A confirmation link goes to the user's CURRENT address,
    // so a stolen session cannot move the account to an attacker's email -- only
    // the existing owner can approve (clicking the link applies the change). In
    // dev / without an email provider, accounts are unverified and no confirmation
    // can be delivered, so the change applies directly (updateEmailWithoutVerification).
    user: {
      changeEmail: {
        enabled: true,
        updateEmailWithoutVerification: !hasEmail,
        sendChangeEmailConfirmation: async (data: {
          user: { email: string };
          newEmail: string;
          url: string;
        }) => {
          await sendEmail(env, {
            to: data.user.email,
            subject: "Confirm your embody.tools email change",
            html: changeEmailHtml(data.newEmail, data.url)
          });
        }
      }
    },
    // Link the Better Auth user to our app-domain users_profile row on creation.
    // Direct D1 query (we intentionally do NOT touch src/server/db.ts).
    databaseHooks: {
      user: {
        create: {
          after: async (user: {
            id: string;
            email?: string | null;
            name?: string | null;
            image?: string | null;
          }) => {
            const handle = await ensureUserProfile(env.DB, {
              id: user.id,
              email: user.email,
              name: user.name,
              // GitHub OAuth populates user.image with the avatar URL; seed it so
              // social sign-ups get a real avatar immediately. Null for email/pw.
              image: user.image
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
  user: { id: string; email?: string | null; name?: string | null; image?: string | null }
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
        `INSERT OR IGNORE INTO users_profile (id, handle, trust_level, avatar_url)
         VALUES (?, ?, 'verified', ?)`
      )
      .bind(user.id, handle, user.image ?? null)
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

// Transactional email bodies. The branded shell (logo, green theme, site fonts)
// lives in src/server/emailTemplate.ts and is shared with the owner notices in
// notifications.ts, so the brand can never drift between the two. renderEmail
// escapes the CTA href + fallback URL defensively.
function verificationEmailHtml(url: string): string {
  return renderEmail({
    heading: "Verify your email",
    bodyHtml: bodyParagraph(
      "Confirm your email address to finish setting up your embody.tools account."
    ),
    cta: { href: url, label: "Verify email" },
    fallbackUrl: url,
    footerNote: "If you did not request this, you can safely ignore this email."
  });
}

function changeEmailHtml(newEmail: string, url: string): string {
  return renderEmail({
    heading: "Confirm your email change",
    bodyHtml: bodyParagraph(
      `We received a request to change your embody.tools email to ${newEmail}. Confirm to apply the change. If you did not request this, ignore this email and nothing changes.`
    ),
    cta: { href: url, label: "Confirm email change" },
    fallbackUrl: url,
    footerNote: "If you did not request this, you can safely ignore this email -- your address stays the same."
  });
}

function resetPasswordEmailHtml(url: string): string {
  return renderEmail({
    heading: "Reset your password",
    bodyHtml: bodyParagraph(
      "We received a request to reset the password for your embody.tools account. This link expires in one hour."
    ),
    cta: { href: url, label: "Reset password" },
    fallbackUrl: url,
    footerNote: "If you did not request this, you can safely ignore this email."
  });
}
