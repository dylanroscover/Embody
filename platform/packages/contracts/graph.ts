// FROZEN CONTRACT C6 - the normalized graph the tdn-viewer renders.
// `parseTDN(tdnDict)` (packages/tdn-viewer) produces this; the React Flow backend consumes it.
// TDN already carries absolute positions + input-index connections, so NO layout engine is
// needed - this is pure draw-from-data. Source fields: docs/tdn/specification.md (C7). ASCII only.

export type RGB = [number, number, number]; // each channel 0..1

export interface GraphNode {
  /** Unique within the graph - the operator path (e.g. "base1/noise1"). */
  id: string;
  /** Operator name (last path segment). */
  name: string;
  /** TD operator type, e.g. "noiseTOP". */
  type: string;
  /** Operator family: TOP | CHOP | SOP | DAT | MAT | POP | COMP | OBJECT. */
  family: string;
  /** Tile color (0..1), only when non-default. */
  color?: RGB;
  /** Node tile position in TD coords (x right, y UP). Omitted-from-TDN defaults to 0. */
  x: number;
  y: number;
  /** Tile size if known (TDN may omit; renderer may use a default). */
  w?: number;
  h?: number;
  /**
   * If this op is DOCKED to a host op (callback/info/shader DATs etc.), the host
   * op's id. The renderer tucks docked ops in a tidy row under their host and
   * draws a dock tether instead of a data-flow wire. Undefined for normal ops.
   */
  dock?: string;
  /**
   * Number of operators nested directly inside this op (a COMP's sub-network).
   * Only the SINGLE-LEVEL parse (parseTDNLevel) sets this -- it powers the
   * viewer's drill-down affordance. The flattening parse (parseTDN) leaves it
   * undefined, since it splays every nested op into one plane. 0/undefined =
   * a leaf op with no sub-network to enter.
   */
  childCount?: number;
}

export interface GraphEdge {
  /** Source node id. */
  from: string;
  /** Destination node id. */
  to: string;
  /** Destination input slot index (array position in the TDN `inputs`/`comp_inputs`). */
  inputIndex: number;
  /** True for COMP-level (top/bottom) connectors; false/undefined for standard (left/right). */
  comp?: boolean;
  /**
   * True for a PARAMETER reference, not a wired input: an op-reference parameter
   * (e.g. a Feedback TOP's `top`/Target, a Render TOP's camera/geometry) whose
   * value names another operator. Drawn as a dotted link so the dependency is
   * visible even though it is not a data wire.
   */
  ref?: boolean;
}

export interface GraphAnnotation {
  title?: string;
  text?: string;
  /** Bottom-left corner in TD coords (y UP), plus width/height. */
  x: number;
  y: number;
  w: number;
  h: number;
  color?: RGB;
}

export interface NormalizedGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  annotations: GraphAnnotation[];
  /** Optional renderer hints (family->color map, etc). */
  theme?: Record<string, unknown>;
}
