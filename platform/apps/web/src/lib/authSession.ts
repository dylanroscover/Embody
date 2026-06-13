import { ensureUserProfile, getAuth, type AuthEnv } from "./auth";

// A resolved, app-domain view of the signed-in user: the Better Auth user id
// plus the users_profile handle (the app's stable identity for ownership,
// /u/<handle> pages, and specimen authoring). Shape is a superset of the
// server-side RequestUser ({ id, handle }) so it drops in everywhere.
export interface SessionUser {
  id: string;
  handle: string;
  email: string;
  name: string;
  image: string | null;
  trustLevel: string;
}

// Resolve the current session (if any) into a SessionUser, backfilling the
// users_profile row if it is somehow missing (e.g. a user created before the
// profile hook, or a failed hook insert). Returns null when unauthenticated.
export async function getSessionUser(
  request: Request,
  env: AuthEnv
): Promise<SessionUser | null> {
  let session;
  try {
    const auth = getAuth(env);
    session = await auth.api.getSession({ headers: request.headers });
  } catch (error) {
    console.error("getSessionUser: getSession failed", error);
    return null;
  }

  if (!session?.user) return null;
  const user = session.user;

  let handle: string | null = null;
  let trustLevel = "verified";
  try {
    const row = await env.DB.prepare(
      "SELECT handle, trust_level FROM users_profile WHERE id = ? LIMIT 1"
    )
      .bind(user.id)
      .first<{ handle: string; trust_level: string }>();
    if (row?.handle) {
      handle = row.handle;
      trustLevel = row.trust_level ?? "verified";
    }
  } catch (error) {
    console.error("getSessionUser: profile lookup failed", error);
  }

  // Backfill if the profile row is missing.
  if (!handle) {
    handle = await ensureUserProfile(env.DB, {
      id: user.id,
      email: user.email,
      name: user.name
    });
  }

  if (!handle) return null;

  return {
    id: user.id,
    handle,
    email: user.email ?? "",
    name: user.name ?? "",
    image: user.image ?? null,
    trustLevel
  };
}
