// Fixed-window rate limiter backed by Cloudflare Workers KV. Keyed by an opaque
// caller-supplied key (typically ip + route). Each window is a single KV counter
// that expires when the window does, so there is no sweep and no unbounded
// growth. ASCII only.
//
// Semantics: the FIRST request in a window writes a counter with a TTL equal to
// the window length; subsequent requests in that window increment it. Once the
// count exceeds `limit`, requests are denied until the counter's TTL elapses.
// KV is eventually consistent, so under a burst the effective limit can drift
// slightly above `limit` -- acceptable for abuse mitigation, not for hard quotas.
//
// FAIL OPEN: if KV is absent (local dev, or the binding is unset) the limiter
// allows the request. This keeps DEV/BUILD green with no new secrets while the
// real limit applies in production where the KV binding exists.

export interface RateLimitOptions {
  /** Max requests permitted within the window. */
  limit: number;
  /** Window length in seconds. */
  windowSec: number;
}

export interface RateLimitResult {
  /** True when the request is permitted. */
  ok: boolean;
  /**
   * Seconds the caller should wait before retrying, when ok is false. Suitable
   * for a Retry-After header. Omitted (undefined) when ok is true.
   */
  retryAfter?: number;
}

// KV is optional so callers can pass env.KV directly without a guard; a falsy
// namespace (dev) yields an immediate allow.
type MaybeKv = KVNamespace | undefined | null;

interface WindowState {
  count: number;
  // Epoch seconds at which the current window ends (when the KV TTL fires).
  resetAt: number;
}

export async function checkRateLimit(
  kv: MaybeKv,
  key: string,
  options: RateLimitOptions
): Promise<RateLimitResult> {
  // No KV (dev) -> allow. Also guard against a non-positive config rather than
  // dividing by zero or locking everyone out.
  if (!kv || options.limit <= 0 || options.windowSec <= 0) {
    return { ok: true };
  }

  const storageKey = `rl:${key}`;
  const nowSec = Math.floor(Date.now() / 1000);

  let state: WindowState | null = null;
  try {
    state = await kv.get<WindowState>(storageKey, "json");
  } catch {
    // KV read failure -> fail open rather than block a legitimate user.
    return { ok: true };
  }

  // Start a fresh window when there is no state or the prior window has elapsed.
  if (!state || typeof state.count !== "number" || state.resetAt <= nowSec) {
    const resetAt = nowSec + options.windowSec;
    try {
      await kv.put(
        storageKey,
        JSON.stringify({ count: 1, resetAt } satisfies WindowState),
        { expirationTtl: options.windowSec }
      );
    } catch {
      // Write failure -> allow; the limiter is best-effort, never a hard gate.
      return { ok: true };
    }
    return { ok: true };
  }

  // Within the active window: deny once the count is at/over the limit.
  if (state.count >= options.limit) {
    return { ok: false, retryAfter: Math.max(1, state.resetAt - nowSec) };
  }

  // Otherwise increment, preserving the original window TTL so the window does
  // not slide forward on every request. Keep the remaining TTL (>= 1s).
  const remainingTtl = Math.max(1, state.resetAt - nowSec);
  try {
    await kv.put(
      storageKey,
      JSON.stringify({ count: state.count + 1, resetAt: state.resetAt } satisfies WindowState),
      { expirationTtl: remainingTtl }
    );
  } catch {
    return { ok: true };
  }

  return { ok: true };
}

// Rate limiting is a PRODUCTION abuse control. In development it only throttles
// the developer and -- critically -- the e2e suite, which bursts hundreds of
// auth/search/copy requests from a single origin (and miniflare DOES populate
// CF-Connecting-IP locally, so an IP-presence check is NOT enough to skip dev).
// Gate every limiter on this so dev/e2e never rate-limits itself, while prod is
// unaffected. Mirrors `rateLimit: { enabled: ENVIRONMENT === "production" }` on
// the Better Auth side.
export function rateLimitDisabled(env: { ENVIRONMENT?: string } | undefined | null): boolean {
  return env?.ENVIRONMENT === "development";
}
