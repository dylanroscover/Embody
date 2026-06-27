import { env } from "cloudflare:workers";
import { SUBMIT_CATEGORIES, SUBMIT_DIFFICULTIES, SUBMIT_REQUIRES } from "@embody/contracts";
import { detectObviousMalware, scanTdn } from "@embody/scanner-ts";
import { parse as parseYaml } from "yaml";
import type { APIRoute } from "astro";
import {
  addSpecimenVersion,
  deleteSpecimenById,
  getCurrentTdnBlobForSlug,
  getSpecimenBySlug,
  getSpecimenForEdit,
  updateSpecimenMetadata,
  type SpecimenEditData
} from "../../../server/db";
import { getParsedTdnForSlug } from "../../../server/tdn";
import { requireUser } from "../../../server/auth";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";
import { byteLength, getTdn, putTdn } from "../../../server/r2";

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

// Owner-only edit. Metadata (title/description/tags/license/difficulty/category/
// requires) always updates. If a NEW tdn is supplied AND differs from the current
// network, it re-runs the SAME safety gates as submit (parse -> capability scan ->
// obvious-malware block) and, only if it clears them, stores the blob + appends a
// new specimen_versions row (addSpecimenVersion).
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

    // Current parsed TDN -- the FTS mirror's dat_text source (syncSpecimensFts
    // replaces the whole row). Overwritten with the new network below if it changes.
    let parsedTdn: Record<string, unknown> | null = null;
    try {
      parsedTdn = (await getParsedTdnForSlug(env.DB, env.BLOBS, slug))?.tdn ?? null;
    } catch {
      parsedTdn = null;
    }

    // ---- Network (TDN) change -----------------------------------------------
    // Re-scan + re-version ONLY when a real change arrives. The owner is already
    // authenticated + ownership-checked above, so no turnstile here.
    let newVersion: number | undefined;
    if (typeof body.value.tdn === "string") {
      const newTdn = body.value.tdn.trim();
      let currentText: string | null = null;
      try {
        const blob = await getCurrentTdnBlobForSlug(env.DB, slug);
        currentText = blob ? await getTdn(env.BLOBS, blob.key) : null;
      } catch {
        currentText = null;
      }

      if (newTdn && newTdn !== (currentText?.trim() ?? "")) {
        const parsed = parseTdn(newTdn);
        if (!parsed.ok) {
          return errorResponse(400, "invalid_tdn", parsed.detail);
        }

        const scan = scanTdn(parsed.tdn);
        if (scan.verdict === "blocked") {
          return jsonResponse(
            {
              error: "scan_blocked",
              detail: "The edited TDN includes blocked capability surfaces.",
              scan
            },
            { status: 422 }
          );
        }

        const malware = detectObviousMalware(parsed.tdn);
        if (malware.malicious) {
          return jsonResponse(
            {
              error: "rejected_malware",
              detail: "The edited TDN contains an unambiguously malicious pattern and was rejected.",
              reasons: malware.reasons,
              scan
            },
            { status: 422 }
          );
        }

        const stored = await putTdn(env.BLOBS, newTdn);
        const result = await addSpecimenVersion(env.DB, {
          specimenId: auth.specimen.id,
          slug: auth.specimen.slug,
          authorHandle: auth.specimen.authorHandle,
          title: body.value.title,
          description: body.value.description,
          tags: body.value.tags,
          tdnR2Key: stored.key,
          tdnSha256: stored.sha256,
          sizeBytes: byteLength(newTdn),
          scan,
          parsedTdn: parsed.tdn
        });
        newVersion = result.versionNum;
        parsedTdn = parsed.tdn; // metadata FTS below mirrors the new network
      }
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

    return jsonResponse({ updated: true, slug: auth.specimen.slug, version: newVersion });
  } catch {
    return serverErrorResponse();
  }
};

// TDN is YAML v2.0 (a strict JSON superset). Mirrors the submit endpoint's gate.
function parseTdn(
  value: string
): { ok: true; tdn: Record<string, unknown> } | { ok: false; detail: string } {
  let parsed: unknown;
  try {
    parsed = parseYaml(value) as unknown;
  } catch {
    return { ok: false, detail: "tdn must be valid YAML or JSON." };
  }
  if (!isRecord(parsed)) {
    return { ok: false, detail: "tdn must parse to a mapping (object)." };
  }
  return { ok: true, tdn: parsed };
}

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
  /** New TDN body. Present only when the owner edited the network. */
  tdn?: string;
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

  // Optional new network body; only present when the owner edited the TDN. Full
  // validation (parse + scan) happens in the PUT handler when it actually changed.
  const tdn = typeof raw.tdn === "string" ? raw.tdn : undefined;

  return {
    ok: true,
    value: {
      title: title.slice(0, 160),
      description: description.slice(0, 4000),
      tags,
      license: license.slice(0, 80),
      difficulty,
      category,
      requires,
      tdn
    }
  };
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
