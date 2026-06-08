import { env } from "cloudflare:workers";
import { scanTdn } from "@embody/scanner-ts";
import type { SubmitRequest, SubmitResponse } from "@embody/contracts";
import type { APIRoute } from "astro";
import {
  insertSpecimenWithVersion,
  listSpecimens,
  normalizeSpecimenSort
} from "../../../server/db";
import { requireUser } from "../../../server/auth";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";
import { byteLength, putTdn, putThumbnail } from "../../../server/r2";
import { verifyTurnstile } from "../../../server/turnstile";

export const prerender = false;

export const GET: APIRoute = async ({ url }) => {
  try {
    const response = await listSpecimens(env.DB, {
      sort: normalizeSpecimenSort(url.searchParams.get("sort")),
      tag: url.searchParams.get("tag") ?? undefined,
      author: url.searchParams.get("author") ?? undefined,
      page: parsePositiveInteger(url.searchParams.get("page")),
      pageSize: parsePositiveInteger(url.searchParams.get("pageSize"))
    });

    return jsonResponse(response, {
      headers: {
        "Cache-Control": "public, max-age=60, s-maxage=300"
      }
    });
  } catch (error) {
    console.error("GET /api/specimens failed", error);
    return serverErrorResponse();
  }
};

export const POST: APIRoute = async ({ request }) => {
  try {
    const body = await readSubmitRequest(request);
    if (!body.ok) {
      return errorResponse(400, "invalid_request", body.detail);
    }

    let user;
    try {
      user = await requireUser(request, env);
    } catch {
      return errorResponse(401, "authentication_required", "A signed-in user is required.");
    }

    const turnstileOk = await verifyTurnstile(
      body.request.turnstileToken,
      env.TURNSTILE_SECRET,
      env.ENVIRONMENT
    );
    if (!turnstileOk) {
      return errorResponse(403, "turnstile_failed", "Turnstile verification failed.");
    }

    const parsedTdn = parseTdnJson(body.request.tdn);
    if (!parsedTdn.ok) {
      return errorResponse(400, "invalid_tdn", parsedTdn.detail);
    }

    const scan = scanTdn(parsedTdn.tdn);
    if (scan.verdict === "blocked") {
      return jsonResponse(
        {
          error: "scan_blocked",
          detail: "The submitted TDN includes blocked capability surfaces.",
          scan
        },
        { status: 422 }
      );
    }

    const blob = await putTdn(env.BLOBS, body.request.tdn);
    const thumbnail = await putThumbnail(env.BLOBS, body.request.thumbnail);
    const inserted = await insertSpecimenWithVersion(env.DB, {
      user,
      title: body.request.title,
      description: body.request.description,
      tags: body.request.tags,
      license: body.request.license,
      tdnR2Key: blob.key,
      tdnSha256: blob.sha256,
      sizeBytes: byteLength(body.request.tdn),
      scan,
      thumbnailKey: thumbnail?.key,
      parsedTdn: parsedTdn.tdn
    });

    const response: SubmitResponse = {
      slug: inserted.slug,
      scan
    };

    return jsonResponse(response, {
      status: 201,
      headers: {
        Location: `/api/specimens/${inserted.slug}`
      }
    });
  } catch {
    return serverErrorResponse();
  }
};

function parsePositiveInteger(value: string | null): number | undefined {
  if (!value) return undefined;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
}

async function readSubmitRequest(
  request: Request
): Promise<{ ok: true; request: SubmitRequest } | { ok: false; detail: string }> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return { ok: false, detail: "Request body must be valid JSON." };
  }

  if (!isRecord(raw)) {
    return { ok: false, detail: "Request body must be a JSON object." };
  }

  const title = readString(raw.title).trim();
  const description = readString(raw.description).trim();
  const license = readString(raw.license).trim() || "CC-BY-4.0";
  const tdn = readString(raw.tdn);
  const turnstileToken = readString(raw.turnstileToken);
  const tags = Array.isArray(raw.tags)
    ? raw.tags
        .filter((tag): tag is string => typeof tag === "string")
        .map((tag) => tag.trim())
        .filter(Boolean)
        .slice(0, 20)
    : [];

  if (!title) return { ok: false, detail: "title is required." };
  if (!tdn) return { ok: false, detail: "tdn is required." };
  if (!turnstileToken) return { ok: false, detail: "turnstileToken is required." };

  const thumbnail = typeof raw.thumbnail === "string" ? raw.thumbnail : undefined;

  return {
    ok: true,
    request: {
      title: title.slice(0, 160),
      description: description.slice(0, 4000),
      tags,
      license: license.slice(0, 80),
      tdn,
      thumbnail,
      turnstileToken
    }
  };
}

function parseTdnJson(
  value: string
): { ok: true; tdn: Record<string, unknown> } | { ok: false; detail: string } {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!isRecord(parsed)) {
      return { ok: false, detail: "tdn must parse to a JSON object." };
    }
    return { ok: true, tdn: parsed };
  } catch {
    return { ok: false, detail: "tdn must be valid JSON." };
  }
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
