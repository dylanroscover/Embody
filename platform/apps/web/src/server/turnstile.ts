interface TurnstileVerifyResponse {
  success?: boolean;
}

export async function verifyTurnstile(
  token: string | undefined,
  secret: string,
  environment = "production"
): Promise<boolean> {
  // Fail closed: the gate only relaxes when we KNOW we are in local development.
  // ONLY the explicit "development" environment bypasses; an unknown / unset /
  // misspelled / staging value verifies a real token (treated as production).
  // Previously this was `environment === "production"`, which inverted the
  // intent -- any non-"production" string (e.g. "staging", "") bypassed the
  // gate. The "dev-bypass" magic token is therefore honored ONLY in development.
  const isDev = environment === "development";
  if (isDev) return true;
  if (!token || !secret) return false;

  const body = new FormData();
  body.append("secret", secret);
  body.append("response", token);

  try {
    const response = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      body,
      // Bound siteverify so a hung Cloudflare endpoint cannot stall submit for
      // the whole Worker budget.
      signal: AbortSignal.timeout(8000)
    });
    if (!response.ok) return false;
    // Parse INSIDE the try: a 200 with a non-JSON body must fail closed, not
    // throw an unhandled rejection past the verifier.
    const result = (await response.json()) as TurnstileVerifyResponse;
    return result.success === true;
  } catch {
    return false;
  }
}
