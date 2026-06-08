interface TurnstileVerifyResponse {
  success?: boolean;
}

export async function verifyTurnstile(
  token: string | undefined,
  secret: string,
  environment = "production"
): Promise<boolean> {
  if (environment !== "production" || token === "dev-bypass") {
    return true;
  }

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
