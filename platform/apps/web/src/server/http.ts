import type { ErrorResponse } from "@embody/contracts";

export function jsonResponse<T>(
  body: T,
  init: ResponseInit = {}
): Response {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");

  return new Response(JSON.stringify(body), {
    ...init,
    headers
  });
}

export function errorResponse(
  status: number,
  error: string,
  detail?: string,
  extraHeaders?: HeadersInit
): Response {
  const body: ErrorResponse = detail ? { error, detail } : { error };
  return jsonResponse(body, {
    status,
    headers: extraHeaders
  });
}

export function serverErrorResponse(): Response {
  return errorResponse(500, "internal_error", "Unexpected server error.");
}
