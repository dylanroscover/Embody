import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getAuth, type AuthEnv } from "../../../lib/auth";

// Better Auth catch-all handler. Mounts every Better Auth endpoint under
// /api/auth/* (sign-up/email, sign-in/email, sign-out, get-session, the GitHub
// OAuth callback when wired, etc.). The auth instance is built lazily here so
// the D1 binding (env.DB) is read inside the request context.
export const prerender = false;

export const ALL: APIRoute = async (ctx) => {
  const auth = getAuth(env as unknown as AuthEnv);
  return auth.handler(ctx.request);
};
