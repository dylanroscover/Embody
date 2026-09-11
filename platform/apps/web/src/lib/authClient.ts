import { createAuthClient } from "better-auth/client";

// Browser-side Better Auth client. Same-origin: the catch-all route is mounted
// at /api/auth, which is Better Auth's default basePath, so no baseURL needed.
export const authClient = createAuthClient();

// A failed call resolves { status, statusText, message?, code? }; a 5xx with an
// empty body has no message, so pages fell through to their 4xx copy ("invalid
// email or password.") while auth was down (field 2026-09-11). Only a 4xx may
// use the server message / fallback; 5xx or a thrown fetch -> unavailable.
export function authUnavailable(what: string): string {
  return `${what} is unavailable right now. please try again in a few minutes.`;
}

export function authErrorMessage(
  error: { status?: number; message?: string } | null | undefined,
  what: string,
  fallback: string
): string {
  const status = error?.status ?? 0;
  // The catch-all limiter's 429 body is { error, detail } -- no message.
  if (status === 429) return error?.message || "too many attempts. please wait a few minutes and try again.";
  if (status >= 400 && status < 500) return error?.message || fallback;
  return authUnavailable(what);
}
