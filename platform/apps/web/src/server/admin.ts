// Admin gating spine -- the single source of truth for "who is an admin" plus
// the small validation/CSRF helpers every /admin page and /api/admin route uses.
//
// Hybrid model: a user is an admin when their trust_level is 'admin' OR their
// email is in the ADMIN_EMAILS allowlist (a comma-separated Worker var/secret).
// NO email is hardcoded -- ADMIN_EMAILS is the only allowlist source. Bootstrap
// the first admin by setting ADMIN_EMAILS (or by giving a user trust_level
// 'admin' directly); from there an admin promotes others inside the panel.
//
// IMPORTANT: src/server/auth.ts requireUser/getRequestUser return only
// { id, handle } -- they DROP email + trustLevel. So API gating MUST resolve the
// full SessionUser via getSessionUser (this module's getAdminUser does that).
// Page guards can use isAdmin(Astro.locals.user, env) directly -- middleware
// already put the full SessionUser on locals.

import { getSessionUser, type SessionUser } from "../lib/authSession";
import type { AuthEnv } from "../lib/auth";
import { errorResponse } from "./http";

// isAdmin / getAdminUser take the full CloudflareEnv. The generated runtime
// `env` from cloudflare:workers is assignable to it (as src/server/auth.ts's
// requireUser already relies on), and only ADMIN_EMAILS is read here.

// ADMIN_EMAILS (comma-separated) -> a lowercased Set. This is the ONLY allowlist
// source -- no email is hardcoded. Empty/unset means the allowlist is empty, so
// admin access then depends solely on a user's trust_level being 'admin'.
function adminAllowlist(env: CloudflareEnv): Set<string> {
  return new Set(
    (env.ADMIN_EMAILS ?? "")
      .split(",")
      .map((e) => e.trim().toLowerCase())
      .filter(Boolean)
  );
}

// Pure predicate over an already-resolved user (page side: Astro.locals.user).
export function isAdmin(
  user: Pick<SessionUser, "email" | "trustLevel"> | null,
  env: CloudflareEnv
): boolean {
  if (!user) return false;
  if (user.trustLevel === "admin") return true;
  const email = (user.email ?? "").trim().toLowerCase();
  return email !== "" && adminAllowlist(env).has(email);
}

// API side: resolve the FULL SessionUser from the request (requireUser drops
// email/trustLevel, so we must use getSessionUser), then apply isAdmin. Returns
// the SessionUser when the caller is an admin, else null.
export async function getAdminUser(
  request: Request,
  env: CloudflareEnv
): Promise<SessionUser | null> {
  if (!env?.DB || !env.BETTER_AUTH_SECRET) return null;
  // CloudflareEnv structurally carries everything AuthEnv needs; the cast mirrors
  // src/middleware.ts (getSessionUser is typed for AuthEnv, not CloudflareEnv).
  const user = await getSessionUser(request, env as unknown as AuthEnv);
  if (!isAdmin(user, env)) return null;
  return user;
}

// API guard: returns the admin SessionUser, or THROWS a Response (a 404 -- a
// non-admin shouldn't even be able to confirm that /api/admin/* exists). Route
// handlers catch it: `try { user = await requireAdmin(...) } catch (res) { return res as Response }`.
export async function requireAdmin(request: Request, env: CloudflareEnv): Promise<SessionUser> {
  const user = await getAdminUser(request, env);
  if (!user) {
    throw errorResponse(404, "not_found", "Not found.");
  }
  return user;
}

// Defense-in-depth CSRF guard for admin mutations: reject a request whose Origin
// header host differs from the request host. fetch() from the admin pages always
// sends a same-origin Origin; a cross-site forged POST would not match. A missing
// Origin (some same-origin navigations) is allowed -- the session cookie is
// SameSite=Lax, so cross-site top-level POSTs don't carry it anyway. Returns an
// error Response to short-circuit, or null when the origin is acceptable.
export function assertSameOrigin(request: Request): Response | null {
  const origin = request.headers.get("origin");
  if (!origin) return null;
  try {
    if (new URL(origin).host !== new URL(request.url).host) {
      return errorResponse(403, "forbidden", "Cross-origin request rejected.");
    }
  } catch {
    return errorResponse(403, "forbidden", "Invalid origin.");
  }
  return null;
}

// --- Bounded vocabularies (server-side validation; mirror the DDL comments) ---

export const REPORT_STATUSES = ["open", "reviewing", "actioned", "dismissed"] as const;
export const VISIBILITIES = ["public", "unlisted", "private"] as const;
export const TIERS = ["community", "verified", "featured"] as const;
export const TRUST_LEVELS = ["anon", "verified", "curator", "admin"] as const;
export const EMAIL_TEMPLATES = [
  "verification",
  "reset-password",
  "new-signup",
  "new-specimen",
  "new-report"
] as const;

export type ReportStatus = (typeof REPORT_STATUSES)[number];
export type Visibility = (typeof VISIBILITIES)[number];
export type SpecimenTier = (typeof TIERS)[number];
export type AdminTrustLevel = (typeof TRUST_LEVELS)[number];
export type EmailTemplate = (typeof EMAIL_TEMPLATES)[number];

export function isReportStatus(v: unknown): v is ReportStatus {
  return typeof v === "string" && (REPORT_STATUSES as readonly string[]).includes(v);
}
export function isVisibility(v: unknown): v is Visibility {
  return typeof v === "string" && (VISIBILITIES as readonly string[]).includes(v);
}
export function isTier(v: unknown): v is SpecimenTier {
  return typeof v === "string" && (TIERS as readonly string[]).includes(v);
}
export function isTrustLevel(v: unknown): v is AdminTrustLevel {
  return typeof v === "string" && (TRUST_LEVELS as readonly string[]).includes(v);
}
export function isEmailTemplate(v: unknown): v is EmailTemplate {
  return typeof v === "string" && (EMAIL_TEMPLATES as readonly string[]).includes(v);
}

// Loose email-shape check for the test-send recipient (not full RFC 5322 -- just
// enough to reject obvious junk before handing it to Resend).
export function isEmailAddress(v: unknown): v is string {
  return typeof v === "string" && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v.trim());
}
