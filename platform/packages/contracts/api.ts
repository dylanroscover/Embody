// FROZEN CONTRACT C4 - the JSON API surface. BOTH the web app and the Embody TD extension
// call these. Changing a shape is a contract bump. ASCII only.
import type { CapabilityJson } from "./capability";

export type Difficulty = "starter" | "intermediate" | "advanced";
export type Tier = "community" | "verified" | "featured";

export interface SpecimenSummary {
  slug: string;
  name: string;
  category: string;
  difficulty: Difficulty;
  description: string;
  /** "none" | "MediaPipe" | "Kinect Azure" | ... */
  requires: string;
  op_count: number;
  /** R2 key for the thumbnail. */
  thumbnail_key: string;
  author_handle: string;
  tier: Tier;
  likes_count: number;
  views_count: number;
}

export interface SpecimenDetail extends SpecimenSummary {
  /** Latest scan result (drives the capability summary UI). */
  capability: CapabilityJson;
  current_version: number;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export interface ListResponse {
  specimens: SpecimenSummary[];
  count: number;
  page: number;
  pageSize: number;
}

export interface SearchResponse {
  results: SpecimenSummary[];
  mode: "keyword" | "semantic";
}

export interface SubmitRequest {
  title: string;
  description: string;
  tags: string[];
  license: string;
  /** JSON string of the TDN dict (the raw network). */
  tdn: string;
  /** Optional data-URL thumbnail; otherwise generated server-side. */
  thumbnail?: string;
  /** Cloudflare Turnstile token, validated server-side. */
  turnstileToken: string;
}

export interface SubmitResponse {
  slug: string;
  /** The server-side scan; a "blocked" verdict means the submit was rejected. */
  scan: CapabilityJson;
}

export interface ErrorResponse {
  error: string;
  detail?: string;
}

/**
 * Endpoint catalog (documentation of the surface; each Worker route implements one).
 * GET /api/specimens/:slug/tdn returns the raw .tdn bytes from R2 (egress-free) with the
 * capability summary in headers - the RETRIEVE endpoint the Embody extension hits.
 */
export const API_ROUTES = {
  list: "GET /api/specimens",
  search: "GET /api/search",
  detail: "GET /api/specimens/:slug",
  tdn: "GET /api/specimens/:slug/tdn",
  submit: "POST /api/specimens",
  like: "POST /api/specimens/:slug/like",
  report: "POST /api/specimens/:slug/report",
} as const;
