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
  // Effective avatar to render (users_profile.avatar_url): a GitHub avatar seeded
  // at sign-up / backfilled here, or an uploaded one, or null -> letter chip.
  avatarUrl: string | null;
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
  let avatarUrl: string | null = null;
  try {
    const row = await env.DB.prepare(
      "SELECT handle, trust_level, avatar_url, banned FROM users_profile WHERE id = ? LIMIT 1"
    )
      .bind(user.id)
      .first<{ handle: string; trust_level: string; avatar_url: string | null; banned: number }>();
    // Banned accounts resolve to NULL everywhere -- they appear logged-out, so
    // they cannot sign in, submit, or reach any authed surface. This single
    // chokepoint IS the ban (reversible: unban -> they resolve again).
    if (row?.banned) return null;
    if (row?.handle) {
      handle = row.handle;
      trustLevel = row.trust_level ?? "verified";
      avatarUrl = row.avatar_url;
    }
  } catch (error) {
    console.error("getSessionUser: profile lookup failed", error);
  }

  // Backfill if the profile row is missing (created before the hook, or a failed
  // hook insert). ensureUserProfile seeds the GitHub avatar too.
  if (!handle) {
    handle = await ensureUserProfile(env.DB, {
      id: user.id,
      email: user.email,
      name: user.name,
      image: user.image
    });
    avatarUrl = user.image ?? null;
  }

  if (!handle) return null;

  // Lazy GitHub-avatar backfill: an account that predates avatars (or signed up
  // before user.image resolved) gets its avatar_url seeded on first authed
  // request. Only fills a NULL -- never clobbers an explicit upload. Best-effort.
  if (!avatarUrl && user.image) {
    avatarUrl = user.image;
    try {
      await env.DB.prepare(
        "UPDATE users_profile SET avatar_url = ? WHERE id = ? AND avatar_url IS NULL"
      )
        .bind(user.image, user.id)
        .run();
    } catch (error) {
      console.error("getSessionUser: avatar backfill failed", error);
    }
  }

  return {
    id: user.id,
    handle,
    email: user.email ?? "",
    name: user.name ?? "",
    image: user.image ?? null,
    trustLevel,
    avatarUrl
  };
}
