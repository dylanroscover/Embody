// FROZEN CONTRACT C2 - TDN capability scan output.
// Produced by the scanner in BOTH languages (packages/scanner-ts AND dev/embody/Embody/Collection/scanner.py)
// from identical inputs; they MUST agree on the same TDN fixtures.
// Consumed by: the Embody import summary, the web capability UI, and D1 `scans.capability_json`.
// Surface definitions: platform/SCANNER-SPEC.md (contract C8). ASCII only.

export type ScanVerdict = "clean" | "flagged" | "blocked";

/** Counts of executable / side-effecting surfaces found in a TDN payload. */
export interface CapabilityCounts {
  /** Execute-family DATs whose content runs on create()/onStart() at import. */
  execute_dats: number;
  /** Parameter expressions (`=` / `~` mode) that read files. */
  file_read_exprs: number;
  /** IO/network operator types (Web Client/Server DAT, TCP/IP, OSC, Touch In/Out, Run, ...). */
  web_ops: number;
  /** COMP extensions (auto-initialized backing DATs run module-level / onInitTD code). */
  extensions: number;
  /** storage / startup_storage payloads restored on import. */
  storage_payloads: number;
  /** Operator types on the IO/network denylist. */
  denylisted_types: number;
  /** file/syncfile params with absolute or traversal (`..`) paths. */
  traversal_paths: number;
  /** COMPs using tdn_ref/tox_ref - they reference EXTERNAL content not present in this payload,
   *  so it cannot be scanned. Community submissions must be self-contained (no external_refs). */
  external_refs: number;
}

export type CapabilitySurface = keyof CapabilityCounts;

export interface ScanFinding {
  /** Operator path within the TDN (e.g. "base1/execute1"). */
  op_path: string;
  /** Which surface this finding belongs to. */
  surface: CapabilitySurface;
  /** Short human-readable reason it was flagged. */
  detail: string;
  /** The offending snippet (expression text, op type, path) - bounded to <= 200 chars. */
  evidence: string;
}

export interface CapabilityJson {
  /** Scanner version that produced this result (for reproducibility). */
  scanner_version: string;
  verdict: ScanVerdict;
  counts: CapabilityCounts;
  findings: ScanFinding[];
}

export const CAPABILITY_SURFACES: readonly CapabilitySurface[] = [
  "execute_dats",
  "file_read_exprs",
  "web_ops",
  "extensions",
  "storage_payloads",
  "denylisted_types",
  "traversal_paths",
  "external_refs",
] as const;

/** A zeroed CapabilityCounts. */
export function emptyCapabilityCounts(): CapabilityCounts {
  return {
    execute_dats: 0,
    file_read_exprs: 0,
    web_ops: 0,
    extensions: 0,
    storage_payloads: 0,
    denylisted_types: 0,
    traversal_paths: 0,
    external_refs: 0,
  };
}
