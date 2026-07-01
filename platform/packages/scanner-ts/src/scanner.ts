import {
  CAPABILITY_SURFACES,
  emptyCapabilityCounts
} from "@embody/contracts";
import type {
  CapabilityCounts,
  CapabilityJson,
  CapabilitySurface,
  ScanFinding,
  ScanVerdict
} from "@embody/contracts";

type TdnRecord = Record<string, unknown>;

const MAX_SERIALIZED_TDN_BYTES = 5 * 1024 * 1024;
const MAX_OPERATORS = 50000;
const EVIDENCE_LIMIT = 200;

const DENYLIST_SEED_TYPES = [
  "webclientDAT",
  "webserverDAT",
  "tcpipDAT",
  "udpinDAT",
  "udpoutDAT",
  "oscinDAT",
  "oscoutDAT",
  "serialDAT",
  "runDAT",
  "executeDAT",
  "datexecuteDAT",
  "chopexecuteDAT",
  "parameterexecuteDAT",
  "parametergroupexecuteDAT",
  "panelexecuteDAT",
  "opexecuteDAT",
  "moviefileinTOP",
  "moviefileoutTOP",
  "folderDAT",
  "touchinTOP",
  "touchoutTOP",
  "webRenderTOP",
  "ndi*",
  "syphonspout*"
] as const;

const DENYLIST_NORMALIZED = new Set(
  DENYLIST_SEED_TYPES.filter((type) => !type.endsWith("*")).map(typeKey)
);

const DANGEROUS_IDENTIFIERS = new Set([
  "eval",
  "exec",
  "compile",
  "__import__",
  "import",
  "from",
  "os",
  "sys",
  "subprocess",
  "socket",
  "shutil",
  "pathlib",
  "open",
  "requests",
  "urllib",
  "run",
  "save",
  "store",
  "mod",
  "tdu",
  "getattr",
  "setattr",
  "globals",
  "locals"
]);

const FILE_EXPR_IDENTIFIERS = new Set(["open", "os", "pathlib", "read", "file"]);

const PATH_PARAM_NAMES = new Set([
  "file",
  "syncfile",
  "filepath",
  "filename",
  "folder",
  "directory",
  "dir",
  "path"
]);

const PATH_STYLES = new Set(["File", "FileSave", "Folder"]);
const WINDOWS_ABSOLUTE_PATH_RE = /^[A-Za-z]:[/\\]/;
const IDENTIFIER_RE = /[A-Za-z_][A-Za-z0-9_]*/g;

interface ScanState {
  counts: CapabilityCounts;
  findings: ScanFinding[];
  blocked: boolean;
}

interface SourceScanResult {
  flagged: boolean;
  detail: string;
}

export function scanTdn(
  tdn: Record<string, unknown>,
  scannerVersion = "v6-scan-ts-1"
): CapabilityJson {
  const counts = emptyCapabilityCounts();
  const findings: ScanFinding[] = [];

  const serializedSize = serializedSizeBytes(tdn);
  if (serializedSize === undefined) {
    findings.push(
      finding(
        "/",
        "storage_payloads",
        "TDN could not be serialized safely for scanner bounds",
        "serialization failed"
      )
    );
    return capability(scannerVersion, "blocked", counts, findings);
  }

  if (serializedSize > MAX_SERIALIZED_TDN_BYTES) {
    findings.push(
      finding(
        "/",
        "storage_payloads",
        "Serialized TDN exceeds 5 MB scanner bound",
        `${serializedSize} bytes`
      )
    );
    return capability(scannerVersion, "blocked", counts, findings);
  }

  try {
    const opBound = operatorCountExceeds(tdn, MAX_OPERATORS);
    if (opBound.exceeds) {
      findings.push(
        finding(
          "/",
          "denylisted_types",
          "TDN operator count exceeds scanner bound",
          `${opBound.count} operators`
        )
      );
      return capability(scannerVersion, "blocked", counts, findings);
    }
  } catch (error) {
    findings.push(
      finding(
        "/",
        "execute_dats",
        "scanner aborted on internal error; failing closed (treat as unsafe)",
        errorName(error)
      )
    );
    return capability(scannerVersion, "blocked", counts, findings);
  }

  const state: ScanState = {
    counts,
    findings,
    blocked: false
  };

  try {
    scanTdnRoot(tdn, state);
  } catch (error) {
    state.blocked = true;
    findings.push(
      finding(
        "/",
        "execute_dats",
        "scanner aborted on internal error; failing closed (treat as unsafe)",
        errorName(error)
      )
    );
  }

  const verdict = state.blocked
    ? "blocked"
    : CAPABILITY_SURFACES.some((surface) => counts[surface] > 0)
      ? "flagged"
      : "clean";

  return capability(scannerVersion, verdict, counts, findings);
}

function capability(
  scannerVersion: string,
  verdict: ScanVerdict,
  counts: CapabilityCounts,
  findings: ScanFinding[]
): CapabilityJson {
  return {
    scanner_version: scannerVersion,
    verdict,
    counts: orderedCounts(counts),
    findings
  };
}

function orderedCounts(counts: CapabilityCounts): CapabilityCounts {
  const ordered = emptyCapabilityCounts();
  for (const surface of CAPABILITY_SURFACES) {
    ordered[surface] = Math.trunc(counts[surface] ?? 0);
  }
  return ordered;
}

function scanTdnRoot(tdn: unknown, state: ScanState): void {
  if (!isRecord(tdn)) return;

  const typeDefaults = isRecord(tdn.type_defaults) ? tdn.type_defaults : {};
  const path = rootPath(tdn);
  scanOperatorLike(tdn, path, typeDefaults, state);

  for (const child of safeList(tdn.operators)) {
    if (isRecord(child)) {
      scanOperator(child, path, typeDefaults, state);
    }
  }
}

function scanOperator(
  opData: TdnRecord,
  parentPath: string,
  typeDefaults: TdnRecord,
  state: ScanState
): void {
  const name = typeof opData.name === "string" && opData.name ? opData.name : "<unnamed>";
  const opPath = joinPath(parentPath, name);
  scanOperatorLike(opData, opPath, typeDefaults, state);

  for (const child of safeList(opData.children)) {
    if (isRecord(child)) {
      scanOperator(child, opPath, typeDefaults, state);
    }
  }
}

function scanOperatorLike(
  opData: TdnRecord,
  opPath: string,
  typeDefaults: TdnRecord,
  state: ScanState
): void {
  const opType = safeString(opData.type);
  const params = effectiveParameters(opData, typeDefaults, opType);

  if (isDenylistedType(opType)) {
    addCount(
      state,
      opPath,
      "web_ops",
      "Operator type is an IO or network surface",
      opType
    );
    addCount(
      state,
      opPath,
      "denylisted_types",
      "Operator type is on the scanner denylist",
      opType
    );
  }

  scanExecuteDat(opData, opPath, opType, state);
  scanDatContentTokens(opData, opPath, opType, state);
  scanParameters(params, opPath, state);
  scanCustomParameters(opData.custom_pars, opPath, state);
  scanSequences(opData.sequences, opPath, opType, state);
  scanStorage(opData, opPath, state);
  scanExternalRefs(opData, opPath, state);
}

function scanExternalRefs(opData: TdnRecord, opPath: string, state: ScanState): void {
  for (const key of ["tdn_ref", "tox_ref"] as const) {
    const ref = opData[key];
    if (typeof ref === "string" && ref.trim()) {
      addCount(
        state,
        opPath,
        "external_refs",
        `COMP references external content via ${key} (not inlined, not scanned)`,
        ref
      );
    }
  }
}

function scanExecuteDat(
  opData: TdnRecord,
  opPath: string,
  opType: string,
  state: ScanState
): void {
  if (!isExecuteDatType(opType)) return;

  const content = opData.dat_content;
  if (!hasDatContent(content)) return;

  addCount(
    state,
    opPath,
    "execute_dats",
    "Execute-family DAT has non-empty content",
    datContentToText(content)
  );
}

function scanDatContentTokens(
  opData: TdnRecord,
  opPath: string,
  opType: string,
  state: ScanState
): void {
  const content = opData.dat_content;
  if (typeof content !== "string" || !content.trim()) return;

  const result = scanPythonSource(content, false);
  if (!result.flagged) return;

  const surface: CapabilitySurface = "execute_dats";
  const alreadyCounted = isExecuteDatType(opType) && hasDatContent(content);
  if (!alreadyCounted) {
    state.counts[surface] += 1;
  }

  state.findings.push(
    finding(
      opPath,
      surface,
      result.detail || "DAT content references executable or IO surface",
      content
    )
  );
}

function scanParameters(params: TdnRecord, opPath: string, state: ScanState): void {
  for (const [parName, value] of Object.entries(params)) {
    scanParameterValue(parName, value, opPath, state);
    scanPathParameter(parName, value, opPath, state);
  }
}

function scanParameterValue(
  parName: unknown,
  value: unknown,
  opPath: string,
  state: ScanState
): void {
  const expr = expressionSource(value);
  if (expr === undefined) return;

  const result = scanPythonSource(expr, true);
  if (!result.flagged) return;

  state.counts.file_read_exprs += 1;
  state.findings.push(
    finding(
      opPath,
      "file_read_exprs",
      `Expression parameter ${safeString(parName)} ${result.detail}`,
      expr
    )
  );
}

function scanPathParameter(
  parName: unknown,
  value: unknown,
  opPath: string,
  state: ScanState
): void {
  if (!isPathParamName(parName)) return;

  for (const text of stringValues(value)) {
    if (isAbsoluteOrTraversalPath(text)) {
      addCount(
        state,
        opPath,
        "traversal_paths",
        `Path parameter ${safeString(parName)} is absolute or traverses upward`,
        text
      );
      return;
    }
  }
}

function scanCustomParameters(customPars: unknown, opPath: string, state: ScanState): void {
  if (Array.isArray(customPars)) {
    for (const item of customPars) {
      if (isRecord(item)) {
        scanCustomParameterDef(item, opPath, state);
      }
    }
    return;
  }

  if (!isRecord(customPars)) return;

  for (const page of Object.values(customPars)) {
    if (Array.isArray(page)) {
      for (const item of page) {
        if (isRecord(item)) {
          scanCustomParameterDef(item, opPath, state);
        }
      }
    } else if (isRecord(page)) {
      for (const [key, value] of Object.entries(page)) {
        if (key === "$t") continue;
        scanParameterValue(key, value, opPath, state);
        scanPathParameter(key, value, opPath, state);
      }
    }
  }
}

function scanCustomParameterDef(
  parDef: TdnRecord,
  opPath: string,
  state: ScanState
): void {
  const name = parDef.name;
  const style = typeof parDef.style === "string" ? parDef.style : "";
  const shouldScanPath = PATH_STYLES.has(style) || isPathParamName(name);

  if (Object.prototype.hasOwnProperty.call(parDef, "value")) {
    const value = parDef.value;
    scanParameterValue(name, value, opPath, state);
    if (shouldScanPath) {
      scanPathParameter(name, value, opPath, state);
    }
  }

  if (!Array.isArray(parDef.values)) return;

  for (const value of parDef.values) {
    scanParameterValue(name, value, opPath, state);
    if (shouldScanPath) {
      scanPathParameter(name, value, opPath, state);
    }
  }
}

function scanSequences(
  sequences: unknown,
  opPath: string,
  opType: string,
  state: ScanState
): void {
  if (!isRecord(sequences)) return;

  if (isCompType(opType) && sequenceHasExtension(sequences.ext)) {
    addCount(
      state,
      opPath,
      "extensions",
      "COMP declares one or more extensions",
      sequences.ext
    );
  }

  for (const blocks of Object.values(sequences)) {
    if (!Array.isArray(blocks)) continue;
    for (const block of blocks) {
      if (!isRecord(block)) continue;
      for (const [key, value] of Object.entries(block)) {
        scanParameterValue(key, value, opPath, state);
        scanPathParameter(key, value, opPath, state);
      }
    }
  }
}

function scanStorage(opData: TdnRecord, opPath: string, state: ScanState): void {
  for (const key of ["storage", "startup_storage"] as const) {
    const payload = opData[key];
    if (hasStoragePayload(payload)) {
      addCount(
        state,
        opPath,
        "storage_payloads",
        `Operator has non-empty ${key}`,
        payload
      );
    }
  }
}

function effectiveParameters(
  opData: TdnRecord,
  typeDefaults: TdnRecord,
  opType: string
): TdnRecord {
  const params: TdnRecord = {};
  const defaultsForType = typeDefaults[opType];
  if (isRecord(defaultsForType) && isRecord(defaultsForType.parameters)) {
    Object.assign(params, defaultsForType.parameters);
  }

  if (isRecord(opData.parameters)) {
    Object.assign(params, opData.parameters);
  }

  return params;
}

function scanPythonSource(source: string, expressionMode: boolean): SourceScanResult {
  if (!source.trim()) {
    return { flagged: false, detail: "" };
  }

  const identifiers = source.match(IDENTIFIER_RE) ?? [];
  for (const identifier of identifiers) {
    if (DANGEROUS_IDENTIFIERS.has(identifier)) {
      return { flagged: true, detail: detailForIdentifier(identifier) };
    }
  }

  if (expressionMode) {
    for (const identifier of identifiers) {
      if (FILE_EXPR_IDENTIFIERS.has(identifier)) {
        return { flagged: true, detail: `references ${identifier}` };
      }
    }
  }

  return { flagged: false, detail: "" };
}

function detailForIdentifier(identifier: string): string {
  if (identifier === "import" || identifier === "from") {
    return "uses an import statement";
  }
  if (identifier === "getattr" || identifier === "setattr" || identifier === "globals" || identifier === "locals") {
    return "uses dynamic attribute access";
  }
  if (identifier === "eval" || identifier === "exec" || identifier === "compile" || identifier === "__import__" || identifier === "open") {
    return `calls ${identifier}`;
  }
  if (identifier === "run" || identifier === "save" || identifier === "store") {
    return `references .${identifier}`;
  }
  return `references ${identifier}`;
}

function expressionSource(value: unknown): string | undefined {
  if (typeof value === "string") {
    if (value.startsWith("==") || value.startsWith("~~")) return undefined;
    if (value.startsWith("=") || value.startsWith("~")) return value.slice(1);
    return undefined;
  }

  if (isRecord(value)) {
    if (typeof value.expr === "string") return value.expr;
    if (typeof value.bind === "string") return value.bind;
  }

  return undefined;
}

function stringValues(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (isRecord(value)) {
    const values: string[] = [];
    if (typeof value.expr === "string") values.push(value.expr);
    if (typeof value.bind === "string") values.push(value.bind);
    return values;
  }
  if (Array.isArray(value)) {
    return value.filter((item): item is string => typeof item === "string");
  }
  return [];
}

function isAbsoluteOrTraversalPath(value: unknown): boolean {
  if (typeof value !== "string") return false;

  let text = stripOuterQuotes(value.trim());
  if (!text) return false;
  if (text.startsWith("=") && !text.startsWith("==")) {
    text = stripOuterQuotes(text.slice(1).trim());
  }
  if (text.startsWith("~") && !text.startsWith("~~")) {
    text = stripOuterQuotes(text.slice(1).trim());
  }

  const normalized = text.replace(/\\/g, "/");
  if (normalized.startsWith("/") || normalized.startsWith("//")) {
    return true;
  }
  if (WINDOWS_ABSOLUTE_PATH_RE.test(text)) {
    return true;
  }

  const parts = normalized.split("/").filter(Boolean);
  return parts.includes("..");
}

function stripOuterQuotes(text: string): string {
  return text.replace(/^['"]+|['"]+$/g, "");
}

function isPathParamName(name: unknown): boolean {
  if (typeof name !== "string") return false;
  const key = name.toLowerCase();
  return (
    PATH_PARAM_NAMES.has(key) ||
    key.endsWith("file") ||
    key.endsWith("path") ||
    key.endsWith("folder")
  );
}

function sequenceHasExtension(extSequence: unknown): boolean {
  if (!Array.isArray(extSequence)) return false;

  for (const block of extSequence) {
    if (!isRecord(block)) continue;
    for (const key of ["object", "name"] as const) {
      const value = block[key];
      if (typeof value === "string" && value.trim()) {
        return true;
      }
    }
  }

  return false;
}

function hasStoragePayload(payload: unknown): boolean {
  if (isRecord(payload)) return Object.keys(payload).length > 0;
  if (Array.isArray(payload)) return payload.length > 0;
  if (typeof payload === "string") return Boolean(payload.trim());
  return payload !== null && payload !== undefined;
}

function hasDatContent(content: unknown): boolean {
  if (typeof content === "string") return Boolean(content.trim());
  if (!Array.isArray(content)) return false;

  for (const row of content) {
    if (Array.isArray(row)) {
      if (row.some((cell) => typeof cell === "string" && cell.trim())) {
        return true;
      }
    } else if (typeof row === "string" && row.trim()) {
      return true;
    }
  }

  return false;
}

function datContentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((row) => {
        if (Array.isArray(row)) {
          return row.map((cell) => safeString(cell)).join("\t");
        }
        return safeString(row);
      })
      .join("\n");
  }
  return safeString(content);
}

function addCount(
  state: ScanState,
  opPath: string,
  surface: CapabilitySurface,
  detail: string,
  evidence: unknown
): void {
  state.counts[surface] += 1;
  state.findings.push(finding(opPath, surface, detail, evidence));
}

function finding(
  opPath: unknown,
  surface: CapabilitySurface,
  detail: unknown,
  evidenceValue: unknown
): ScanFinding {
  return {
    op_path: safeString(opPath) || "/",
    surface,
    detail: safeString(detail),
    evidence: evidence(evidenceValue)
  };
}

function evidence(value: unknown): string {
  let text: string;
  if (typeof value === "string") {
    text = value;
  } else {
    try {
      text = JSON.stringify(value) ?? safeString(value);
    } catch {
      text = safeString(value);
    }
  }

  text = text.split(/\s+/).filter(Boolean).join(" ");
  if (text.length > EVIDENCE_LIMIT) {
    return `${text.slice(0, EVIDENCE_LIMIT - 3)}...`;
  }
  return text;
}

function serializedSizeBytes(value: unknown): number | undefined {
  try {
    const text = JSON.stringify(value);
    if (text === undefined) return undefined;
    return new TextEncoder().encode(text).length;
  } catch {
    return undefined;
  }
}

function operatorCountExceeds(
  tdn: unknown,
  cap: number
): { exceeds: boolean; count: number } {
  if (!isRecord(tdn)) {
    return { exceeds: false, count: 0 };
  }

  let count = 1;
  const stack = [...safeList(tdn.operators)].reverse();
  const seen = new WeakSet<object>();

  while (stack.length > 0) {
    const item = stack.pop();
    if (!isRecord(item)) continue;
    if (seen.has(item)) continue;
    seen.add(item);

    count += 1;
    if (count > cap) {
      return { exceeds: true, count };
    }

    stack.push(...safeList(item.children).reverse());
  }

  return { exceeds: false, count };
}

function isDenylistedType(opType: string): boolean {
  const key = typeKey(opType);
  if (DENYLIST_NORMALIZED.has(key)) return true;
  if (key.endsWith("executedat")) return true;
  if (key.startsWith("ndi")) return true;
  if (key.startsWith("syphonspout")) return true;
  if (key.startsWith("web") && (key.endsWith("dat") || key.endsWith("top"))) {
    return true;
  }
  return false;
}

function isExecuteDatType(opType: string): boolean {
  const key = typeKey(opType);
  return key === "executedat" || key.endsWith("executedat");
}

function isCompType(opType: string): boolean {
  return typeKey(opType).endsWith("comp");
}

function typeKey(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function rootPath(tdn: TdnRecord): string {
  if (typeof tdn.network_path === "string" && tdn.network_path) {
    return tdn.network_path;
  }
  if (typeof tdn.name === "string" && tdn.name) {
    return tdn.name;
  }
  return "/";
}

function joinPath(parentPath: unknown, childName: unknown): string {
  const parent = safeString(parentPath);
  const child = safeString(childName) || "<unnamed>";
  if (!parent || parent === "/") return child;
  return `${parent.replace(/\/+$/g, "")}/${child}`;
}

function safeList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function safeString(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return String(value);
  } catch {
    return "";
  }
}

function errorName(error: unknown): string {
  if (error instanceof Error && error.name) return error.name;
  return safeString(error) || "Error";
}

function isRecord(value: unknown): value is TdnRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
