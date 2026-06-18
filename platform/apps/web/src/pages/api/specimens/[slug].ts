import { env } from "cloudflare:workers";
import { SUBMIT_CATEGORIES, SUBMIT_DIFFICULTIES, SUBMIT_REQUIRES } from "@embody/contracts";
import type { APIRoute } from "astro";
import {
  deleteSpecimenById,
  getSpecimenBySlug,
  getSpecimenForEdit,
  updateSpecimenMetadata,
  type SpecimenEditData
} from "../../../server/db";
import { getParsedTdnForSlug } from "../../../server/tdn";
import { requireUser } from "../../../server/auth";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";

export const prerender = false;

export const GET: APIRoute = async ({ params }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    const specimen = await getSpecimenBySlug(env.DB, slug);
    if (!specimen) {
      return errorResponse(404, "specimen_not_found", "No public specimen exists for that slug.");
    }

    return jsonResponse(specimen, {
      headers: {
        "Cache-Control": "public, max-age=60, s-maxage=300"
      }
    });
  } catch {
    return serverErrorResponse();
  }
};

// Owner-only metadata edit (title/description/tags/license/difficulty/category/
// requires). The TDN body is intentionally NOT editable here -- changing the
// network requires a re-scan + new version (a separate path).
export const PUT: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    const auth = await resolveOwner(request, slug);
    if (!auth.ok) return auth.response;

    const body = await readEditRequest(request);
    if (!body.ok) {
      return errorResponse(400, "invalid_request", body.detail);
    }

    // Re-parse the unchanged current TDN so the FTS mirror keeps its dat_text
    // (syncSpecimensFts replaces the whole row). Best-effort: a missing blob just
    // means the network-content half of search goes empty until the next submit.
    let parsedTdn: Record<string, unknown> | null = null;
    try {
      parsedTdn = (await getParsedTdnForSlug(env.DB, env.BLOBS, slug))?.tdn ?? null;
    } catch {
      parsedTdn = null;
    }

    await updateSpecimenMetadata(env.DB, {
      specimenId: auth.specimen.id,
      slug: auth.specimen.slug,
      authorHandle: auth.specimen.authorHandle,
      title: body.value.title,
      description: body.value.description,
      tags: body.value.tags,
      license: body.value.license,
      difficulty: body.value.difficulty,
      category: body.value.category,
      requires: body.value.requires,
      parsedTdn
    });

    return jsonResponse({ updated: true, slug: auth.specimen.slug });
  } catch {
    return serverErrorResponse();
  }
};

// Owner-only hard delete of a specimen and all of its dependent rows.
export const DELETE: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    const auth = await resolveOwner(request, slug);
    if (!auth.ok) return auth.response;

    await deleteSpecimenById(env.DB, auth.specimen.id);
    return jsonResponse({ deleted: true, slug });
  } catch (error) {
    console.error("DELETE /api/specimens/[slug] failed", error);
    return serverErrorResponse();
  }
};

type OwnerResult =
  | { ok: true; specimen: SpecimenEditData }
  | { ok: false; response: Response };

// Shared gate for PUT/DELETE: require a session, load the specimen, and confirm
// the signed-in user is its author. Ownership is enforced by author_id (not the
// handle) -- the authoritative key.
async function resolveOwner(request: Request, slug: string): Promise<OwnerResult> {
  let user;
  try {
    user = await requireUser(request, env);
  } catch {
    return {
      ok: false,
      response: errorResponse(401, "authentication_required", "A signed-in user is required.")
    };
  }

  const specimen = await getSpecimenForEdit(env.DB, slug);
  if (!specimen) {
    return {
      ok: false,
      response: errorResponse(404, "specimen_not_found", "No specimen exists for that slug.")
    };
  }

  if (specimen.authorId !== user.id) {
    return {
      ok: false,
      response: errorResponse(403, "forbidden", "You can only modify your own specimens.")
    };
  }

  return { ok: true, specimen };
}

interface EditValue {
  title: string;
  description: string;
  tags: string[];
  license: string;
  difficulty: string;
  category: string;
  requires: string;
}

// Validate the edit payload. Mirrors readSubmitRequest's whitelist rules for the
// frozen vocabularies, minus tdn/turnstile/thumbnail (the body is not editable).
async function readEditRequest(
  request: Request
): Promise<{ ok: true; value: EditValue } | { ok: false; detail: string }> {
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
  if (!title) return { ok: false, detail: "title is required." };

  const description = readString(raw.description).trim();
  const license = readString(raw.license).trim() || "CC-BY-4.0";
  const tags = Array.isArray(raw.tags)
    ? raw.tags
        .filter((tag): tag is string => typeof tag === "string")
        .map((tag) => tag.trim())
        .filter(Boolean)
        .slice(0, 20)
    : [];

  const difficulty = readString(raw.difficulty).trim() || "intermediate";
  if (!SUBMIT_DIFFICULTIES.includes(difficulty as (typeof SUBMIT_DIFFICULTIES)[number])) {
    return { ok: false, detail: `difficulty must be one of: ${SUBMIT_DIFFICULTIES.join(", ")}.` };
  }

  const category = readString(raw.category).trim();
  if (category && !SUBMIT_CATEGORIES.includes(category)) {
    return { ok: false, detail: `category must be one of: ${SUBMIT_CATEGORIES.join(", ")}.` };
  }

  const requires = readString(raw.requires).trim() || "none";
  if (!SUBMIT_REQUIRES.includes(requires)) {
    return { ok: false, detail: `requires must be one of: ${SUBMIT_REQUIRES.join(", ")}.` };
  }

  return {
    ok: true,
    value: {
      title: title.slice(0, 160),
      description: description.slice(0, 4000),
      tags,
      license: license.slice(0, 80),
      difficulty,
      category,
      requires
    }
  };
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
