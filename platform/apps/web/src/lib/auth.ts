import { betterAuth } from "better-auth";
import { D1Dialect } from "kysely-d1";

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
      // No email verification flow wired yet (no transactional email provider).
      // Accounts are usable immediately; tighten this once email is available.
      requireEmailVerification: false,
      autoSignIn: true
    },
    ...(socialProviders ? { socialProviders } : {}),
    // Link the Better Auth user to our app-domain users_profile row on creation.
    // Direct D1 query (we intentionally do NOT touch src/server/db.ts).
    databaseHooks: {
      user: {
        create: {
          after: async (user: { id: string; email?: string | null; name?: string | null }) => {
            await ensureUserProfile(env.DB, {
              id: user.id,
              email: user.email,
              name: user.name
            });
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
