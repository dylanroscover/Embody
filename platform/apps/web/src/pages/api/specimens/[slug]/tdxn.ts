import { env } from "cloudflare:workers";
import type { APIRoute } from "astro";
import { getCurrentTdxnBlobForSlug } from "../../../../server/db";
import { errorResponse, jsonResponse, serverErrorResponse } from "../../../../server/http";
import { getParsedTdxnForSlug } from "../../../../server/tdxn";
import { getTdxn } from "../../../../server/r2";

export const prerender = false;

// GET /api/specimens/:slug/tdn
//
// Two shapes off ONE endpoint, selected by ?format=:
//   (default)        Raw .tdn YAML bytes from R2, egress-free, with the scan
//                    verdict in a header. This is the FROZEN C4 contract the
//                    Embody TD extension hits to "embody" a specimen -- DO NOT
//                    change its body or headers.
//   ?format=graph    A trimmed, parsed JSON cover graph (operators + annotations
//                    only, no heavy DAT/shader text or parameters). This is what
//                    each collection card LAZY-FETCHES as it scrolls in, so the
//                    page never bundles thousands of graphs at build time. The
//                    payload is a few KB and feeds straight into TdxnViewer.
export const GET: APIRoute = async ({ params, url }) => {
  try {
    const slug = params.slug;
    if (!slug) {
      return errorResponse(400, "invalid_slug", "A specimen slug is required.");
    }

    if (url.searchParams.get("format") === "graph") {
      return await graphResponse(slug);
    }

    const blob = await getCurrentTdxnBlobForSlug(env.DB, slug);
    if (!blob) {
      return errorResponse(404, "tdxn_not_found", "No TDXN blob exists for that slug.");
    }

    const tdxn = await getTdxn(env.BLOBS, blob.key);
    if (!tdxn) {
      return errorResponse(404, "tdxn_not_found", "The TDXN blob is missing from R2.");
    }

    return new Response(tdxn, {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "public, max-age=300, s-maxage=3600",
        "X-Embody-Scan-Verdict": blob.capability.verdict
      }
    });
  } catch {
    return serverErrorResponse();
  }
};

// Resolve, parse, and trim the current-version TDXN into a light cover graph.
async function graphResponse(slug: string): Promise<Response> {
  const parsed = await getParsedTdxnForSlug(env.DB, env.BLOBS, slug);
  if (!parsed) {
    return errorResponse(404, "tdxn_not_found", "No TDXN graph exists for that slug.");
  }

  return jsonResponse(trimTdxnForCover(parsed.tdxn), {
    headers: {
      // Cover graphs are immutable per content-addressed version; cache hard.
      "Cache-Control": "public, max-age=300, s-maxage=3600"
    }
  });
}

// Operator fields TdxnViewer renders. Everything else (parameters, dat_content,
// shader text, storage) is dropped so the cover graph stays a few KB.
const OPERATOR_KEYS = [
  "name",
  "type",
  "position",
  "size",
  "color",
  "inputs",
  "comp_inputs",
  "dock"
] as const;

function trimTdxnForCover(tdxn: Record<string, unknown>): Record<string, unknown> {
  const graph: Record<string, unknown> = {
    format: typeof tdxn.format === "string" ? tdxn.format : "tdxn",
    version: typeof tdxn.version === "string" ? tdxn.version : "2.0"
  };
  if (typeof tdxn.type === "string") graph.type = tdxn.type;

  if (Array.isArray(tdxn.operators)) {
    graph.operators = trimOperatorList(tdxn.operators);
  }
  if (Array.isArray(tdxn.annotations)) {
    graph.annotations = tdxn.annotations;
  }

  return graph;
}

function trimOperatorList(operators: unknown[]): Record<string, unknown>[] {
  const trimmed: Record<string, unknown>[] = [];

  for (const op of operators) {
    if (!op || typeof op !== "object" || Array.isArray(op)) continue;
    const source = op as Record<string, unknown>;
    const node: Record<string, unknown> = {};

    for (const key of OPERATOR_KEYS) {
      if (source[key] !== undefined) node[key] = source[key];
    }

    // Recurse into nested COMP children so sub-networks still draw.
    if (Array.isArray(source.operators)) {
      node.operators = trimOperatorList(source.operators);
    }
    if (Array.isArray(source.annotations)) {
      node.annotations = source.annotations;
    }

    trimmed.push(node);
  }

  return trimmed;
}
