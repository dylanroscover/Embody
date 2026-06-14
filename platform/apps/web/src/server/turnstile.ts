interface TurnstileVerifyResponse {
  success?: boolean;
}

export async function verifyTurnstile(
  token: string | undefined,
  secret: string,
  environment = "production"
): Promise<boolean> {
  // Fail closed: the gate only relaxes when we KNOW we are not in production.
  // An unknown / unset environment is treated as production (verify a real
  // token). The "dev-bypass" magic token is honored ONLY outside production --
  // in production it is just another token that must pass siteverify, so an
  // authenticated user can no longer POST turnstileToken:"dev-bypass" to skip
  // the gate.
  const isProd = environment === "production";
  if (!isProd) return true;
  if (!token || !secret) return false;

  const body = new FormData();
  body.append("secret", secret);
  body.append("response", token);

  let response: Response;
  try {
    response = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      body
    });
  } catch {
    return false;
  }

  if (!response.ok) return false;

  const result = (await response.json()) as TurnstileVerifyResponse;
  return result.success === true;
}
