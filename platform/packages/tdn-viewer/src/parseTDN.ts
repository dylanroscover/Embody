import type {
  GraphAnnotation,
  GraphEdge,
  GraphNode,
  NormalizedGraph,
  RGB
} from "@embody/contracts";

type TdnDict = Record<string, unknown>;

const FAMILIES = ["TOP", "CHOP", "SOP", "DAT", "MAT", "POP", "COMP"] as const;

// A network's child operators. TDN names them `operators` at the document root
// and `children` inside a nested COMP -- accept either (and both, defensively),
// so the same walk works at any depth. Order (children before operators) matches
// the original flatten recursion so the dense view stays byte-identical.
function networkOperators(dict: TdnDict): TdnDict[] {
  return [...asRecords(dict.children), ...asRecords(dict.operators)];
}

/**
 * Descend the raw TDN tree to the sub-network at `path` (each segment a COMP
 * name); the empty path is the document root. Returns null if a segment names no
 * operator at its level. The returned dict is the network whose operators the
 * viewer should draw -- feed it straight to parseTDNLevel via the path.
 */
export function getNetworkAtPath(tdn: TdnDict, path: string[]): TdnDict | null {
  let net: TdnDict = tdn;
  for (const segment of path) {
    const match = networkOperators(net).find((op) => readString(op.name) === segment);
    if (!match) return null;
    net = match;
  }
  return net;
}

/**
 * Parse the WHOLE network into one flat graph, recursing into every nested COMP
 * so all descendants are drawn at once. This is the dense, all-in-one-plane view
 * (homepage hero, card-cover thumbnails). For the navigable, TD-faithful
 * one-level-at-a-time view, use parseTDNLevel instead.
 */
export function parseTDN(tdn: TdnDict): NormalizedGraph {
  return buildGraph(networkOperators(tdn), asRecords(tdn.annotations), true);
}

/**
 * Parse a SINGLE network level -- the sub-network at `path` (COMP names), or the
 * root when path is empty. COMPs are NOT recursed into: each is drawn as one tile
 * carrying a `childCount` so the viewer can offer drill-down. This is the 1:1
 * representation of a TD network, navigated like TD's own editor (enter a COMP,
 * climb back out). Returns an empty graph if the path doesn't resolve.
 */
export function parseTDNLevel(tdn: TdnDict, path: string[]): NormalizedGraph {
  const net = getNetworkAtPath(tdn, path);
  if (!net) return { nodes: [], edges: [], annotations: [] };
  return buildGraph(networkOperators(net), asRecords(net.annotations), false);
}

// Shared engine for both parses. `recurse` decides whether a COMP's children are
// walked into the same graph (flatten) or summarized as a `childCount` on the
// COMP's own node (single level).
function buildGraph(
  rootOperators: TdnDict[],
  rootAnnotations: TdnDict[],
  recurse: boolean
): NormalizedGraph {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const annotations: GraphAnnotation[] = [];
  // Op-reference parameters (e.g. a Feedback TOP's `top`/Target) are resolved
  // AFTER every node exists, since the referenced op may be defined later.
  const paramRefs: { parentPath: string; value: string; to: string }[] = [];

  walkOperators(rootOperators, "");
  collectAnnotations(rootAnnotations, annotations);
  resolveParamRefs();

  return { nodes, edges, annotations };

  // A parameter whose value names another operator (a Feedback TOP's Target, a
  // Render TOP's camera/geometry, etc.) is a real dependency the wires don't
  // draw. Collect plain-string param values; resolveParamRefs() keeps only those
  // that name an actual node and emits a dotted `ref` edge for each.
  function collectParamRefs(op: TdnDict, parentPath: string, opId: string): void {
    const params = isRecord(op.parameters)
      ? op.parameters
      : isRecord(op.pars)
        ? op.pars
        : null;
    if (!params) return;
    for (const value of Object.values(params)) {
      if (typeof value === "string" && value && !value.startsWith("=")) {
        paramRefs.push({ parentPath, value, to: opId });
      }
    }
  }

  function resolveParamRefs(): void {
    const nodeIds = new Set(nodes.map((n) => n.id));
    const seen = new Set(edges.map((e) => `${e.from} ${e.to}`));
    // A docked op is already linked to its host by a dock tether, and an op's
    // shader/info-DAT parameters (a glslTOP's `pixeldat`, an infoDAT's `op`)
    // point right back at those docks -- skip ref edges across any host<->dock
    // pair so we don't double-draw the tether.
    const dockPairs = new Set<string>();
    for (const n of nodes) {
      if (n.dock) {
        dockPairs.add(`${n.dock} ${n.id}`);
        dockPairs.add(`${n.id} ${n.dock}`);
      }
    }
    for (const { parentPath, value, to } of paramRefs) {
      const from = resolveInputPath(parentPath, value);
      if (!from || from === to || !nodeIds.has(from)) continue;
      const key = `${from} ${to}`;
      if (seen.has(key) || dockPairs.has(key)) continue; // don't duplicate a wire/dock
      seen.add(key);
      edges.push({ from, to, inputIndex: 0, ref: true });
    }
  }

  function walkOperators(operators: TdnDict[], parentPath: string): void {
    for (const op of operators) {
      const name = readString(op.name);
      if (!name) continue;

      const id = joinPath(parentPath, name);
      const type = readString(op.type) || "unknown";

      // An annotateCOMP IS the annotation -- its title/body are also emitted in
      // the top-level `annotations` array, which renders as the text box. Drawing
      // the COMP as its own node tile is pure redundancy, so skip it here. (Its
      // annotation still appears via collectAnnotations on the array.)
      if (type.toLowerCase() === "annotatecomp") continue;

      const position = readPair(op.position);
      const size = readOptionalPair(op.size);
      const color = readRGB(op.color);

      const node: GraphNode = {
        id,
        name,
        type,
        family: deriveFamily(type),
        x: position[0],
        y: position[1]
      };

      if (color) node.color = color;
      if (size) {
        node.w = size[0];
        node.h = size[1];
      }

      // Docked op (callback/info/shader DATs etc.): `dock` names the host op,
      // relative to this op's network. Resolve it to the host's id so the
      // renderer can tuck it under the host and draw a tether.
      const dockName = readString(op.dock);
      if (dockName) {
        const hostId = resolveInputPath(parentPath, dockName);
        if (hostId) node.dock = hostId;
      }

      const childOperators = networkOperators(op);

      // Single-level mode: a COMP stays one tile, tagged with how many ops live
      // inside so the viewer can offer a drill-in affordance -- its children are
      // NOT walked, and its internal annotations belong to that sub-network, not
      // this level. Flatten mode: walk children into this same graph below.
      if (!recurse && childOperators.length > 0) {
        node.childCount = childOperators.length;
      }

      nodes.push(node);

      collectEdges(asStrings(op.inputs), parentPath, id, false, edges);
      collectEdges(asStrings(op.comp_inputs), parentPath, id, true, edges);
      collectParamRefs(op, parentPath, id);

      if (recurse) {
        collectAnnotations(asRecords(op.annotations), annotations);
        if (childOperators.length > 0) {
          walkOperators(childOperators, id);
        }
      }
    }
  }
}

function collectEdges(
  inputPaths: string[],
  parentPath: string,
  targetId: string,
  comp: boolean,
  edges: GraphEdge[]
): void {
  inputPaths.forEach((inputPath, inputIndex) => {
    const sourceId = resolveInputPath(parentPath, inputPath);
    if (!sourceId) return;

    const edge: GraphEdge = {
      from: sourceId,
      to: targetId,
      inputIndex
    };

    if (comp) edge.comp = true;
    edges.push(edge);
  });
}

function collectAnnotations(items: TdnDict[], annotations: GraphAnnotation[]): void {
  for (const item of items) {
    const position = readPair(item.position);
    const size = readPair(item.size);
    const color = readRGB(item.color);
    const annotation: GraphAnnotation = {
      x: position[0],
      y: position[1],
      w: size ? size[0] : 0,
      h: size ? size[1] : 0
    };

    const title = readString(item.title) || readString(item.name);
    const text = readString(item.text);
    if (title) annotation.title = title;
    if (text) annotation.text = text;
    if (color) annotation.color = color;

    annotations.push(annotation);
  }
}

function deriveFamily(type: string): string {
  const upper = type.toUpperCase();
  for (const family of FAMILIES) {
    if (upper.endsWith(family)) return family;
  }
  return "OBJECT";
}

function resolveInputPath(parentPath: string, inputPath: string): string | undefined {
  const trimmed = inputPath.trim();
  if (!trimmed) return undefined;

  if (trimmed.startsWith("/")) {
    return normalizeSegments(trimmed.slice(1).split("/"));
  }

  if (trimmed.includes("/")) {
    const base = parentPath ? parentPath.split("/") : [];
    return normalizeSegments([...base, ...trimmed.split("/")]);
  }

  return joinPath(parentPath, trimmed);
}

function normalizeSegments(segments: string[]): string {
  const normalized: string[] = [];
  for (const segment of segments) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      normalized.pop();
      continue;
    }
    normalized.push(segment);
  }
  return normalized.join("/");
}

function joinPath(parentPath: string, name: string): string {
  return parentPath ? `${parentPath}/${name}` : name;
}

function asRecords(value: unknown): TdnDict[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord);
}

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function readPair(value: unknown): [number, number] {
  if (!Array.isArray(value)) return [0, 0];
  const x = typeof value[0] === "number" ? value[0] : 0;
  const y = typeof value[1] === "number" ? value[1] : 0;
  return [x, y];
}

function readOptionalPair(value: unknown): [number, number] | undefined {
  if (!Array.isArray(value)) return undefined;
  const x = value[0];
  const y = value[1];
  if (typeof x !== "number" || typeof y !== "number") return undefined;
  return [x, y];
}

function readRGB(value: unknown): RGB | undefined {
  if (!Array.isArray(value)) return undefined;
  const r = value[0];
  const g = value[1];
  const b = value[2];
  if (typeof r !== "number" || typeof g !== "number" || typeof b !== "number") {
    return undefined;
  }
  return [r, g, b];
}

function isRecord(value: unknown): value is TdnDict {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
