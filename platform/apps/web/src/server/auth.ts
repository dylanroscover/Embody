import { getSessionUser } from "../lib/authSession";
import type { AuthEnv } from "../lib/auth";

export interface RequestUser {
  id: string;
  handle: string;
}

// Resolve the signed-in user from the real Better Auth session cookie. Returns
// null when there is no valid session. Backed by src/lib/auth.ts (Better Auth +
// D1) and the users_profile linkage; the stub that always returned a dev user
// has been replaced. Shape is intentionally the narrow { id, handle } that
// db.ts and the specimens API depend on.
export async function getRequestUser(
  request: Request,
  env: CloudflareEnv
): Promise<RequestUser | null> {
  if (!env?.DB || !env.BETTER_AUTH_SECRET) return null;
  const user = await getSessionUser(request, env as unknown as AuthEnv);
  if (!user) return null;
  return { id: user.id, handle: user.handle };
}

export async function requireUser(request: Request, env: CloudflareEnv): Promise<RequestUser> {
  const user = await getRequestUser(request, env);
  if (!user) {
    throw new Error("Authentication required.");
  }
  return user;
}
