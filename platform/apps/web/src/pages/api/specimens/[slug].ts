import { env } from "cloudflare:workers";
import { DEFAULT_LICENSE, MAX_CATEGORIES, SUBMIT_CATEGORIES, SUBMIT_LEVELS, SUBMIT_LICENSE_VALUES, SUBMIT_REQUIRES } from "@embody/contracts";
import { detectObviousMalware, scanTdn } from "@embody/scanner-ts";
import { parse as parseYaml } from "yaml";
import type { APIRoute } from "astro";
import {
  addSpecimenVersion,
  deleteSpecimenById,
  getCurrentTdnBlobForSlug,
  getSpecimenBySlug,
  getSpecimenForEdit,
  setSpecimenVisibility,
  updateSpecimenMetadata,
  type SpecimenEditData
} from "../../../server/db";
import { getParsedTdnForSlug, MAX_TDN_TEXT_CHARS } from "../../../server/tdn";
import { requireUser } from "../../../server/auth";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../server/http";
import { byteLength, getTdn, putThumbnail, putTdn } from "../../../server/r2";

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

// Owner-only edit. Metadata (title/description/tags/license/level/category/
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

    // Existing categories are also accepted on edit (beyond the current
    // whitelist) so a specimen carrying a retired category can still be saved.
    const body = await readEditRequest(request, auth.specimen.categories, auth.specimen.license);
    if (!body.ok) {
      return errorResponse(400, "invalid_request", body.detail);
    }

    // Current parsed TDN -- the FTS mirror's dat_text source (syncSpecimensFts
    // replaces the whole row). Overwritten with the new network below if it changes.
    let parsedTdn: Record<string, unknown> | null = null;
    try {
      parsedTdn = (await getParsedTdnForSlug(env.DB, env.BLOBS, slug, auth.specimen.authorId))?.tdn ?? null;
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
        const blob = await getCurrentTdnBlobForSlug(env.DB, slug, auth.specimen.authorId);
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

    // Optional new cover image. Store it in R2 first; putThumbnail returns null
    // on a missing/malformed/oversized payload, in which case thumbnailKey stays
    // undefined and the existing thumbnail is left untouched.
    let thumbnailKey: string | undefined;
    if (body.value.thumbnail) {
      const stored = await putThumbnail(env.BLOBS, body.value.thumbnail);
      if (stored) thumbnailKey = stored.key;
    }

    await updateSpecimenMetadata(env.DB, {
      specimenId: auth.specimen.id,
      slug: auth.specimen.slug,
      authorHandle: auth.specimen.authorHandle,
      title: body.value.title,
      description: body.value.description,
      tags: body.value.tags,
      license: body.value.license,
      level: body.value.level,
      categories: body.value.categories,
      requires: body.value.requires,
      thumbnailKey,
      parsedTdn
    });

    return jsonResponse({ updated: true, slug: auth.specimen.slug, version: newVersion });
  } catch {
    return serverErrorResponse();
  }
};

// Owner-only publish toggle. Flips a specimen between 'private' (the author's
// draft, hidden from the Collection) and 'public' (live). This is the lightweight
// one-click action behind the Publish / Make-private buttons -- distinct from PUT,
// which is a full metadata/network edit. Only the binary public<->private states
// are user-settable here; 'unlisted' stays a moderator-only state (admin route).
export const PATCH: APIRoute = async ({ params, request }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    const auth = await resolveOwner(request, slug);
    if (!auth.ok) return auth.response;

    let raw: unknown;
    try {
      raw = await request.json();
    } catch {
      return errorResponse(400, "invalid_body", "A JSON body is required.");
    }
    const visibility = (raw as { visibility?: unknown })?.visibility;
    if (visibility !== "public" && visibility !== "private") {
      return errorResponse(
        400,
        "invalid_visibility",
        "visibility must be 'public' or 'private'."
      );
    }

    await setSpecimenVisibility(env.DB, auth.specimen.id, visibility);
    return jsonResponse({ updated: true, slug: auth.specimen.slug, visibility });
  } catch (error) {
    console.error("PATCH /api/specimens/[slug] failed", error);
    return serverErrorResponse();
  }
};

// TDN is YAML v2.0 (a strict JSON superset). Mirrors the submit endpoint's gate.
function parseTdn(
  value: string
): { ok: true; tdn: Record<string, unknown> } | { ok: false; detail: string } {
  // Bound the synchronous parse on the edit path (DoS guard), same cap the read
  // path enforces in parseTdnYaml.
  if (value.length > MAX_TDN_TEXT_CHARS) {
    return { ok: false, detail: "tdn is too large." };
  }
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
  level: string;
  categories: string[];
  requires: string[];
  /** New TDN body. Present only when the owner edited the network. */
  tdn?: string;
  /** New thumbnail as a data URL. Present only when the owner picked an image. */
  thumbnail?: string;
}

// Validate the edit payload. Mirrors readSubmitRequest's whitelist rules for the
// frozen vocabularies, minus turnstile. tdn + thumbnail are optional -- supplied
// only when the owner actually changed the network or the cover image.
async function readEditRequest(
  request: Request,
  existingCategories: string[] = [],
  existingLicense: string = DEFAULT_LICENSE
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
  // License is a fixed dropdown vocabulary. Accept a whitelisted value, or the
  // specimen's CURRENT license (so a legacy/retired value survives an edit);
  // anything else preserves the existing license rather than silently changing it.
  const rawLicense = readString(raw.license).trim();
  const license =
    SUBMIT_LICENSE_VALUES.includes(rawLicense) || rawLicense === existingLicense
      ? rawLicense
      : existingLicense;
  const tags = Array.isArray(raw.tags)
    ? raw.tags
        .filter((tag): tag is string => typeof tag === "string")
        .map((tag) => tag.trim())
        .filter(Boolean)
        .slice(0, 20)
    : [];

  const level = readString(raw.level).trim() || "intermediate";
  if (!SUBMIT_LEVELS.includes(level as (typeof SUBMIT_LEVELS)[number])) {
    return { ok: false, detail: `level must be one of: ${SUBMIT_LEVELS.join(", ")}.` };
  }

  // categories: 1..MAX_CATEGORIES from the whitelist (legacy single `category`
  // coerced to one item). The first is the primary.
  const rawCategories = Array.isArray(raw.categories)
    ? raw.categories
    : typeof raw.category === "string" && raw.category.trim()
      ? [raw.category]
      : [];
  const categories = [
    ...new Set(
      rawCategories
        .filter((c): c is string => typeof c === "string")
        .map((c) => c.trim())
        .filter(Boolean)
    )
  ];
  if (categories.length === 0) {
    return { ok: false, detail: "at least one category is required." };
  }
  if (categories.length > MAX_CATEGORIES) {
    return { ok: false, detail: `choose at most ${MAX_CATEGORIES} categories.` };
  }
  // Allow the current whitelist plus any category the specimen already carries
  // (so a retired taxonomy value can be preserved through an edit), but nothing
  // arbitrary beyond that.
  const allowedCategories = new Set<string>([...SUBMIT_CATEGORIES, ...existingCategories]);
  const unknownCategory = categories.find((c) => !allowedCategories.has(c));
  if (unknownCategory) {
    return { ok: false, detail: `category must be one of: ${SUBMIT_CATEGORIES.join(", ")}.` };
  }

  // requires is a multi-select list (legacy single string coerced to one item).
  const rawRequires = Array.isArray(raw.requires)
    ? raw.requires
    : typeof raw.requires === "string" && raw.requires.trim() && raw.requires.trim() !== "none"
      ? [raw.requires]
      : [];
  const requires = [
    ...new Set(
      rawRequires
        .filter((r): r is string => typeof r === "string")
        .map((r) => r.trim())
        .filter(Boolean)
    )
  ];
  const unknownRequire = requires.find((r) => !SUBMIT_REQUIRES.includes(r));
  if (unknownRequire) {
    return { ok: false, detail: `requires must be values from: ${SUBMIT_REQUIRES.join(", ")}.` };
  }

  // Optional new network body; only present when the owner edited the TDN. Full
  // validation (parse + scan) happens in the PUT handler when it actually changed.
  const tdn = typeof raw.tdn === "string" ? raw.tdn : undefined;

  // Optional replacement thumbnail (a client-resized data URL). putThumbnail
  // re-validates the content type + byte cap and ignores anything malformed.
  const thumbnail = typeof raw.thumbnail === "string" ? raw.thumbnail : undefined;

  return {
    ok: true,
    value: {
      title: title.slice(0, 160),
      description: description.slice(0, 4000),
      tags,
      license: license.slice(0, 80),
      level,
      categories,
      requires,
      tdn,
      thumbnail
    }
  };
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
