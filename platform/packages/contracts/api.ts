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
  /** Zero or more values from SUBMIT_REQUIRES; empty = stock TouchDesigner. */
  requires: string[];
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
  /** One of SUBMIT_CATEGORIES. */
  category: string;
  /** Zero or more values from SUBMIT_REQUIRES; empty = stock TouchDesigner. */
  requires: string[];
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
