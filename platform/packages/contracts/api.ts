// FROZEN CONTRACT C4 - the JSON API surface. BOTH the web app and the Embody TD extension
// call these. Changing a shape is a contract bump. ASCII only.
import type { CapabilityJson } from "./capability";

export type Level = "starter" | "intermediate" | "advanced";
export type Tier = "community" | "verified" | "featured";

export interface SpecimenSummary {
  slug: string;
  name: string;
  category: string;
  level: Level;
  description: string;
  /** "none" | "MediaPipe" | "Kinect Azure" | ... */
  requires: string;
  op_count: number;
  /** R2 key for the thumbnail. */
  thumbnail_key: string;
  author_handle: string;
  tier: Tier;
  /** Total reactions across all emojis (denormalized; drives the "popular" sort). */
  likes_count: number;
  /**
   * Per-emoji reaction tallies keyed by emoji, e.g. {"thumbsup": 5, "fire": 2}
   * (keys are the literal emoji characters). Omitted/empty when there are none.
   */
  reactions?: Record<string, number>;
  views_count: number;
  /** How many times this specimen's TDN envelope has been copied from the website. */
  copies_count: number;
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
  /** Total number of specimens matching the active filter/search (server total). */
  count: number;
  page: number;
  pageSize: number;
  /**
   * Opaque keyset cursor for the NEXT page, or null when the current page is the
   * last. Pass it back as `?cursor=` to fetch the following page in O(page) cost.
   * Present on the cursor-paginated list/collection path; absent on legacy callers.
   */
  nextCursor?: string | null;
}

export interface SearchResponse {
  results: SpecimenSummary[];
  mode: "keyword" | "semantic";
}

// Submit-form metadata vocabularies. These are the whitelists the submit API
// validates against; the submit form renders pickers from the same sets so the
// client and server agree. ASCII only.
export const SUBMIT_LEVELS: readonly Level[] = [
  "starter",
  "intermediate",
  "advanced"
] as const;

// Category taxonomy shared by the submit form and the collection facets,
// spanning the major domains of TouchDesigner work. Free-form categories are NOT
// accepted on submit; a value outside this set is rejected server-side.
export const SUBMIT_CATEGORIES: readonly string[] = [
  "generative",
  "compositing",
  "3d",
  "simulation",
  "raymarching-sdf",
  "audio-reactive",
  "particles",
  "shaders",
  "feedback",
  "data-visualization",
  "interactive",
  "projection-mapping",
  "video",
  "machine-learning",
  "glitch",
  "fractal",
  "typography",
  "audio",
  "animation",
  "ui",
  "utility",
  "networking",
  "hardware",
  "api",
  "system"
] as const;

// Hardware / capability requirement facet. "none" = runs on stock TouchDesigner.
export const SUBMIT_REQUIRES: readonly string[] = [
  "none",
  "MediaPipe",
  "Kinect Azure",
  "Audio"
] as const;

export interface SubmitRequest {
  title: string;
  description: string;
  tags: string[];
  license: string;
  /** One of SUBMIT_LEVELS. */
  level: Level;
  /** One of SUBMIT_CATEGORIES. */
  category: string;
  /** One of SUBMIT_REQUIRES ("none" = stock TouchDesigner). */
  requires: string;
  /** YAML (or legacy JSON) string of the TDN dict (the raw network). */
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
