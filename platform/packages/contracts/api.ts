// FROZEN CONTRACT C4 - the JSON API surface. BOTH the web app and the Embody TD extension
// call these. Changing a shape is a contract bump. ASCII only.
import type { CapabilityJson } from "./capability";

export type Level = "starter" | "intermediate" | "advanced";
export type Tier = "community" | "verified" | "featured";
/**
 * Who can see a specimen. New uploads default to `private` (the author's draft);
 * the author publishes to `public` to add it to the Collection. `unlisted` is a
 * moderator-only state (reachable via the link, hidden from listings).
 */
export type Visibility = "public" | "unlisted" | "private";

export interface SpecimenSummary {
  slug: string;
  name: string;
  /** Primary category (the first of `categories`); backs single-slot display. */
  category: string;
  /** Full category set (1..MAX_CATEGORIES), primary first. Includes `category`. */
  categories: string[];
  level: Level;
  description: string;
  /** Zero or more values from SUBMIT_REQUIRES; empty = stock TouchDesigner. */
  requires: string[];
  op_count: number;
  /** R2 key for the thumbnail. */
  thumbnail_key: string;
  /**
   * R2 key for the cover video (MP4/H.264), when the cover carries one. Null/absent
   * = image-only cover. Video is purely additive: a video cover ALSO sets
   * thumbnail_key with an auto-extracted poster, so this never replaces the poster.
   */
  video_key?: string | null;
  author_handle: string;
  /**
   * The author's avatar image URL when they have one set; null/absent means the
   * consumer should render the letter-initial chip instead.
   */
  author_avatar_url?: string | null;
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
  /**
   * Visibility of the specimen. Present on owner/admin-scoped reads (e.g. a user's
   * own profile) so private drafts can be badged; omitted on public listings,
   * where every row is `public` by construction.
   */
  visibility?: Visibility;
}

export interface SpecimenDetail extends SpecimenSummary {
  /** Latest scan result (drives the capability summary UI). */
  capability: CapabilityJson;
  current_version: number;
  tags: string[];
  /** SPDX-style license identifier (one of SUBMIT_LICENSE_VALUES, or a legacy value). */
  license: string;
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

// A specimen may belong to several categories; this caps how many. The submit
// form enforces it client-side and the API rejects more than this server-side.
export const MAX_CATEGORIES = 3;

// License vocabulary for the submit + edit forms. `value` is the SPDX-style
// identifier we store; `label` is what the dropdown shows. Free-form licenses are
// NOT accepted on submit; a value outside this set is coerced to the default
// server-side. The first entry is the default. Covers the Creative Commons family
// most creative-coding work uses, the common permissive/copyleft code licenses,
// and an explicit all-rights-reserved opt-out.
export const SUBMIT_LICENSES: readonly { value: string; label: string }[] = [
  { value: "CC-BY-4.0", label: "CC BY 4.0 - attribution" },
  { value: "CC-BY-SA-4.0", label: "CC BY-SA 4.0 - attribution, share-alike" },
  { value: "CC-BY-NC-4.0", label: "CC BY-NC 4.0 - attribution, non-commercial" },
  { value: "CC-BY-NC-SA-4.0", label: "CC BY-NC-SA 4.0 - non-commercial, share-alike" },
  { value: "CC0-1.0", label: "CC0 1.0 - public domain" },
  { value: "MIT", label: "MIT" },
  { value: "Apache-2.0", label: "Apache 2.0" },
  { value: "GPL-3.0", label: "GPL 3.0" },
  { value: "All-rights-reserved", label: "All rights reserved" }
] as const;

// Flat whitelist of valid license values, for server-side validation.
export const SUBMIT_LICENSE_VALUES: readonly string[] = SUBMIT_LICENSES.map((l) => l.value);

// The default license when none is supplied or an unknown value arrives.
export const DEFAULT_LICENSE = "CC-BY-4.0";

// Hardware / capability requirements facet. A specimen can require SEVERAL of
// these (multi-select); an EMPTY list means it runs on stock TouchDesigner. The
// submit form renders a grouped (multi-level) checkbox picker from these groups;
// SUBMIT_REQUIRES is the flat whitelist the API validates each selected value
// against. ASCII only. Keep labels short (dropdown-friendly).
export const SUBMIT_REQUIRE_GROUPS: readonly { group: string; items: readonly string[] }[] = [
  { group: "Depth & body tracking", items: [
    "Kinect Azure", "Kinect v2", "Intel RealSense", "Stereolabs ZED", "Orbbec", "Leap Motion", "iPhone/iPad LiDAR", "Luxonis OAK-D"
  ] },
  { group: "Motion capture & eye tracking", items: [
    "OptiTrack", "Vicon", "Rokoko", "Xsens", "Tobii eye tracker", "ARKit face"
  ] },
  { group: "Cameras & video capture", items: [
    "Webcam", "IP camera (RTSP)", "HDMI capture", "Blackmagic DeckLink", "AJA", "Machine-vision camera", "PTZ camera"
  ] },
  { group: "Video over IP & texture sharing", items: [
    "NDI", "Spout (Windows)", "Syphon (macOS)"
  ] },
  { group: "Audio", items: [
    "Audio interface (ASIO)", "Ableton Link", "Virtual audio (BlackHole/VB-Cable)", "Multichannel/spatial audio", "VST plugin", "Dante"
  ] },
  { group: "Control & protocols", items: [
    "OSC", "MIDI controller", "WebSocket", "MQTT", "Serial/Arduino", "Stream Deck", "TouchOSC"
  ] },
  { group: "Lighting & show control", items: [
    "DMX/Art-Net", "sACN", "Lighting console (grandMA)", "LED processor"
  ] },
  { group: "Projection & display", items: [
    "Projector(s)", "Multi-projector warp/blend", "Genlock", "Multi-GPU (Mosaic)"
  ] },
  { group: "VR / AR / XR", items: [
    "SteamVR/OpenVR", "Vive Tracker", "Meta Quest"
  ] },
  { group: "External engines & hosts", items: [
    "TouchEngine", "Notch", "Unreal Engine", "Unity", "Resolume", "MadMapper", "Disguise"
  ] },
  { group: "AI & ML runtimes", items: [
    "MediaPipe", "NVIDIA GPU (CUDA)", "NVIDIA RTX", "StreamDiffusion", "ONNX Runtime", "Stable Diffusion/ComfyUI"
  ] },
  { group: "Cloud AI APIs", items: [
    "OpenAI API", "Anthropic API", "Cloud AI/LLM API", "ElevenLabs", "Runway"
  ] },
  { group: "Microcontrollers & sensors", items: [
    "ESP32", "Raspberry Pi (GPIO)", "IMU sensor", "EEG (Muse)", "LiDAR scanner", "GPS"
  ] },
  { group: "System & platform", items: [
    "Windows only", "macOS only", "TouchDesigner Pro", "High-VRAM GPU", "Internet connection"
  ] },
] as const;

// Flat whitelist derived from the groups -- the set the submit API validates
// each selected requirement against. An empty selection = stock TouchDesigner.
export const SUBMIT_REQUIRES: readonly string[] = SUBMIT_REQUIRE_GROUPS.flatMap((g) => g.items);

export interface SubmitRequest {
  title: string;
  description: string;
  tags: string[];
  license: string;
  /** One of SUBMIT_LEVELS. */
  level: Level;
  /**
   * 1..MAX_CATEGORIES values from SUBMIT_CATEGORIES; the first is the primary.
   * Legacy callers may send a single `category` string instead (accepted but
   * deprecated); the server coerces it to a one-element `categories`.
   */
  categories: string[];
  /** @deprecated Use `categories`. Legacy single-category field. */
  category?: string;
  /** Zero or more values from SUBMIT_REQUIRES; empty = stock TouchDesigner. */
  requires: string[];
  /** YAML (or legacy JSON) string of the TDN dict (the raw network). */
  tdn: string;
  /** Optional data-URL thumbnail; otherwise generated server-side. */
  thumbnail?: string;
  /**
   * Initial visibility. Only 'public' or 'private' are accepted on submit;
   * absent (or anything else) defaults to 'private' -- the author's draft, which
   * they publish to 'public' from the specimen page. ('unlisted' is admin-only.)
   */
  visibility?: Visibility;
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
