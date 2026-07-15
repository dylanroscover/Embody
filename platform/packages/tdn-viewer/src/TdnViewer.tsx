import "@xyflow/react/dist/style.css";
import "./tdnViewer.css";

import {
  Background,
  BackgroundVariant,
  BaseEdge,
  ControlButton,
  Controls,
  getSmoothStepPath,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type EdgeTypes,
  type Node,
  type NodeProps,
  type NodeTypes,
  type ReactFlowInstance
} from "@xyflow/react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, MouseEvent as ReactMouseEvent } from "react";
import type { GraphAnnotation, GraphNode, NormalizedGraph, RGB } from "@embody/contracts";
import { operatorsAtLevel, parseTDN, parseTDNLevel } from "./parseTDN";

/**
 * The operator a viewer click selected, with the raw TDN fields a properties
 * panel needs. Carried both to the optional `onSelect` callback and on a
 * `tdnviewer:select` DOM CustomEvent (detail) so a non-React host (an Astro
 * page) can render the panel without a serialized callback prop. `null` = the
 * selection was cleared (a click on empty canvas).
 */
export interface TdnSelection {
  /** Drill path to the current level (COMP names, deepest last); empty at root. */
  path: string[];
  /** Node id at this level (the operator name). */
  id: string;
  name: string;
  type: string;
  family: string;
  /** True when this is a COMP with a sub-network to enter (double-click). */
  isComp: boolean;
  /** Operators nested directly inside (0 for a leaf op). */
  childCount: number;
  comment?: string;
  tags?: string[];
  /** Built-in parameters the network customized (non-default values only). */
  parameters?: Record<string, unknown>;
  /** Custom parameter pages (COMPs): { pageName: [par, ...] }. */
  customPars?: Record<string, unknown>;
  /** DAT text content (shader source, scripts, tables) when the TDN captured it,
      normalized to a single newline-joined string. Shown as a code block. */
  datContent?: string;
}

/** Mirrors React Flow's `PaddingWithUnit` (it isn't re-exported from
    @xyflow/react), so per-side fitView padding typechecks against fitView. */
type PaddingValue = number | `${number}px` | `${number}%`;
type PaddingObject = {
  top?: PaddingValue;
  right?: PaddingValue;
  bottom?: PaddingValue;
  left?: PaddingValue;
  x?: PaddingValue;
  y?: PaddingValue;
};

export interface TdnViewerProps {
  tdn: Record<string, unknown>;
  className?: string;
  height?: number | string;
  /**
   * Enable COMP drill-down + operator selection: a single click selects any
   * operator (highlighted, and surfaced via onSelect / the tdnviewer:select
   * event for a properties panel), and a double click on a COMP enters its
   * sub-network, with a breadcrumb "address bar" to climb back out -- a 1:1,
   * TD-faithful walk of the network. When false (the default), the whole network
   * is flattened into one inert plane (the dense hero / card-cover thumbnail).
   */
  navigable?: boolean;
  /** Label for the root crumb in the breadcrumb bar (e.g. the specimen name). */
  rootLabel?: string;
  /**
   * Selection callback (navigable only). Also dispatched as a `tdnviewer:select`
   * DOM CustomEvent on document, so an Astro page can subscribe without passing a
   * (non-serializable) function prop into the island.
   */
  onSelect?: (selection: TdnSelection | null) => void;
  /**
   * fitView padding -- a number (all sides, fraction of the viewport) or per-side
   * values with units, e.g. { left: '27%', right: '27%' }. The app shell passes
   * per-side percentages so the network fits the free area BETWEEN the floating
   * panels instead of being clipped behind them. Defaults to a tight 0.24 (used
   * by the flattened card-cover thumbnails, which have no panels).
   */
  fitPadding?: PaddingValue | PaddingObject;
  /**
   * Draw annotation (annotateCOMP) boxes. Default true. Set false for a clean,
   * purely-decorative graph (e.g. the landing-page hero background) where a
   * partial annotation box would just be clutter.
   */
  showAnnotations?: boolean;
}

type OperatorNodeData = {
  name: string;
  type: string;
  family: string;
  familyColor: string;
  inputCount: number;
  compInputCount: number;
  /** True when this op hosts docked ops (shows a dock-out connector). */
  isDockHost: boolean;
  /** True when this op is docked to a host (shows a dock-in connector). */
  isDocked: boolean;
  /** Endpoints of a parameter-reference (ref) edge: top handles let the dotted
      link arc over the tiles instead of running flat through the data wires. */
  isRefSource: boolean;
  isRefTarget: boolean;
  /** True when this is a COMP with a sub-network to drill into (navigable view
      only). Drives the clickable cursor + hover state. */
  canEnter: boolean;
  /** True when this op is the current selection (drives the highlight ring). */
  selected: boolean;
};

// Fallback node footprint, used only when an operator (and its type default)
// carries no size in the TDN. Real sizes come straight from the TDN for a 1:1
// layout; see toFlowElements.
const DEFAULT_W = 130;
const DEFAULT_H = 90;

type OperatorNode = Node<OperatorNodeData, "operator">;

type AnnotationNodeData = {
  title: string;
  text: string;
  width: number;
  height: number;
  color: string;
};

type AnnotationNode = Node<AnnotationNodeData, "annotation">;
type TdnFlowNode = OperatorNode | AnnotationNode;

// Operator-family colors follow TouchDesigner's own family palette so each node
// reads with its correct family identity (TOPs purple, CHOPs green, SOPs blue,
// etc.) -- NOT an arbitrary assignment. The reference hexes in the comments are
// TD's exact `ui.colors[<FAMILY>]` values (read live from the app); the values
// used here are those hues brightened/saturated so they pop as the vivid header
// bar on the dark viewer, since TD's are muted node-body tints. COMP is lifted
// from TD's near-black grey (#303030) to a legible neutral.
const FAMILY_COLORS: Record<string, string> = {
  TOP: "#9d8cdb", // TD #695c93 -- blue-purple
  CHOP: "#7cc45a", // TD #628c46 -- green
  SOP: "#5fa3dd", // TD #4a80b2 -- blue
  POP: "#7a78ec", // TD #504ebf -- blue-violet (bluer than TOP)
  MAT: "#c9b85f", // TD #9f9447 -- olive-gold
  DAT: "#c684ad", // TD #935c80 -- mauve / pinky-purple
  COMP: "#9aa1a8", // TD #303030 -- neutral grey (lifted for legibility)
  OBJECT: "#b9b09d" // fallback for any unrecognized family
};

const NODE_TYPES: NodeTypes = {
  annotation: memo(AnnotationBox),
  operator: memo(OperatorTile)
};

// A data wire that would run straight across an intervening same-row tile is hard
// to read. toFlowElements detects that case and routes the edge through this
// component with a raised `data.liftY`, so the wire arcs just above the tile it
// passes (the side handles are unchanged -- only the middle segment rises).
function LiftedEdge({
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  style,
  data
}: EdgeProps) {
  const liftY = (data as { liftY?: number } | undefined)?.liftY;
  let path: string;
  if (typeof liftY === "number") {
    // A same-row Right->Left edge has no vertical step, so getSmoothStepPath's
    // `centerY` can't bend it -- it renders dead flat across the intervening
    // tile. Instead draw a smooth cubic arch: the wire leaves the source and
    // enters the target horizontally (control points offset along x), and both
    // control points sit high enough that the curve's midpoint peaks at ~liftY.
    // For a symmetric cubic with sourceY == targetY, the t=0.5 height is
    // sourceY + 0.75*(ctrlY - sourceY), so ctrlY = sourceY + (liftY - sourceY)/0.75
    // makes the peak land on liftY.
    const dx = Math.max(40, Math.abs(targetX - sourceX) * 0.35);
    const ctrlY = sourceY + (liftY - sourceY) / 0.75;
    path = `M ${sourceX},${sourceY} C ${sourceX + dx},${ctrlY} ${targetX - dx},${ctrlY} ${targetX},${targetY}`;
  } else {
    [path] = getSmoothStepPath({
      sourceX,
      sourceY,
      sourcePosition,
      targetX,
      targetY,
      targetPosition,
      borderRadius: 12
    });
  }
  return <BaseEdge path={path} markerEnd={markerEnd} style={style} />;
}

const EDGE_TYPES: EdgeTypes = { lifted: LiftedEdge };

// Respect prefers-reduced-motion for the fitView animation: return 0 (instant,
// no animation) when the user has asked to reduce motion, otherwise the given
// duration. SSR-safe -- matchMedia is only touched in the browser.
function fitViewDuration(duration: number): number {
  if (typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return 0;
  }
  return duration;
}

export function TdnViewer({
  tdn,
  className,
  height = 520,
  navigable = false,
  rootLabel,
  onSelect,
  fitPadding = 0.24,
  showAnnotations = true
}: TdnViewerProps) {
  // Drill-down path: each segment a COMP name, deepest last. Empty = root. Only
  // meaningful when `navigable`; the flatten view ignores it.
  const [path, setPath] = useState<string[]>([]);
  // Currently-selected operator (navigable only); null when nothing is selected.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Mirror of selectedId updated synchronously, so rapid arrow-key presses don't
  // read a stale value between React renders.
  const selectedIdRef = useRef<string | null>(null);
  // A different network entirely (e.g. switching specimens) resets to its root.
  useEffect(() => {
    setPath([]);
    setSelectedId(null);
  }, [tdn]);

  const graph = useMemo(
    () => (navigable ? parseTDNLevel(tdn, path) : parseTDN(tdn)),
    [tdn, path, navigable]
  );
  const { nodes, edges } = useMemo(
    () => toFlowElements(graph, showAnnotations),
    [graph, showAnnotations]
  );

  // Raw operator records at the current level, keyed by node id (the op name),
  // so a selection can carry the op's parameters / custom_pars / comment / tags
  // to the properties panel without re-walking the tree.
  const opRecords = useMemo(() => {
    const map = new Map<string, Record<string, unknown>>();
    if (!navigable) return map;
    for (const op of operatorsAtLevel(tdn, path)) {
      const name = typeof op.name === "string" ? op.name : null;
      if (name) map.set(name, op);
    }
    return map;
  }, [tdn, path, navigable]);

  // Emit a selection to the optional callback AND as a document CustomEvent, so
  // an Astro page (which can't pass a function prop into the island) can render
  // the properties panel by subscribing to `tdnviewer:select`.
  const emitSelect = useCallback(
    (selection: TdnSelection | null) => {
      onSelect?.(selection);
      if (typeof document !== "undefined") {
        document.dispatchEvent(new CustomEvent("tdnviewer:select", { detail: selection }));
      }
    },
    [onSelect]
  );

  // Operators at the current level, keyed by id -- for selection lookups from
  // both clicks and arrow-key navigation.
  const nodeById = useMemo(() => {
    const map = new Map<string, GraphNode>();
    for (const n of graph.nodes) map.set(n.id, n);
    return map;
  }, [graph]);

  const buildSelection = useCallback(
    (id: string): TdnSelection | null => {
      const gn = nodeById.get(id);
      if (!gn) return null;
      const raw = opRecords.get(id);
      const params =
        raw && isPlainRecord(raw.parameters)
          ? raw.parameters
          : raw && isPlainRecord(raw.pars)
            ? (raw.pars as Record<string, unknown>)
            : undefined;
      const customPars = raw && isPlainRecord(raw.custom_pars) ? raw.custom_pars : undefined;
      // DAT text content (a shader's GLSL, a script DAT's Python, a table). The
      // TDN may store it as one string or an array of lines -- normalize to one.
      const rawContent = raw ? raw.dat_content : undefined;
      const datContent =
        typeof rawContent === "string"
          ? rawContent
          : Array.isArray(rawContent)
            ? rawContent.map((line) => (typeof line === "string" ? line : String(line))).join("\n")
            : undefined;
      return {
        path: [...path],
        id,
        name: gn.name,
        type: gn.type,
        family: gn.family,
        isComp: (gn.childCount ?? 0) > 0,
        childCount: gn.childCount ?? 0,
        comment: raw && typeof raw.comment === "string" ? raw.comment : undefined,
        tags: raw && Array.isArray(raw.tags) ? (raw.tags as string[]) : undefined,
        parameters: params,
        customPars: customPars as Record<string, unknown> | undefined,
        datContent
      };
    },
    [nodeById, opRecords, path]
  );

  // Single entry point for changing the selection: updates the ref (sync),
  // React state (highlight), and notifies the panel.
  const applySelection = useCallback(
    (id: string | null) => {
      selectedIdRef.current = id;
      setSelectedId(id);
      emitSelect(id ? buildSelection(id) : null);
    },
    [emitSelect, buildSelection]
  );
  // Keep the ref in sync when selection is cleared by the reset effects below.
  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  // Tag the selected operator so OperatorTile can draw the highlight ring.
  const selectedNodes = useMemo(
    () =>
      nodes.map((node) =>
        node.type === "operator"
          ? { ...node, data: { ...node.data, selected: node.id === selectedId } }
          : node
      ),
    [nodes, selectedId]
  );

  const style = useMemo<CSSProperties>(
    () => ({
      height: typeof height === "number" ? `${height}px` : height
    }),
    [height]
  );

  // Double-click on EMPTY canvas fits the WHOLE graph into view (a reset),
  // instead of React Flow's default zoom-IN (zoomOnDoubleClick is disabled
  // below). A double-click on a node is handled separately (enter a COMP), so
  // bail when the gesture started on a node -- otherwise entering would also fit.
  const rfRef = useRef<ReactFlowInstance<TdnFlowNode, Edge> | null>(null);
  const handleDoubleClick = useCallback((event: ReactMouseEvent) => {
    const target = event.target as HTMLElement | null;
    if (target?.closest?.(".react-flow__node")) return;
    rfRef.current?.fitView({ padding: fitPadding, duration: fitViewDuration(320) });
  }, []);

  // Single click selects any operator (and surfaces its parameters). `event.detail`
  // gates out the 2nd click of a double-click so the selection doesn't fight the
  // enter gesture below.
  const handleNodeClick = useCallback(
    (event: ReactMouseEvent, node: TdnFlowNode) => {
      if (!navigable || event.detail > 1) return;
      if (node.type !== "operator") return;
      applySelection(node.id);
    },
    [navigable, applySelection]
  );

  // Double click a COMP-with-children to descend into its sub-network.
  const handleNodeDoubleClick = useCallback(
    (_event: ReactMouseEvent, node: TdnFlowNode) => {
      if (!navigable || node.type !== "operator" || !node.data.canEnter) return;
      setPath((prev) => [...prev, node.id]);
    },
    [navigable]
  );

  // Click on empty canvas clears the selection.
  const handlePaneClick = useCallback(() => {
    if (!navigable) return;
    applySelection(null);
  }, [navigable, applySelection]);

  // Arrow keys move the selection to the nearest operator in that direction
  // (TD coords: up = +y). A directional score prefers close + axis-aligned ops.
  const arrowSelect = useCallback(
    (dir: "left" | "right" | "up" | "down"): boolean => {
      const currentId = selectedIdRef.current;
      if (!currentId) return false;
      const cur = nodeById.get(currentId);
      if (!cur) return false;
      const cx = cur.x + (cur.w ?? DEFAULT_W) / 2;
      const cy = cur.y + (cur.h ?? DEFAULT_H) / 2;
      let best: GraphNode | null = null;
      let bestScore = Infinity;
      for (const n of nodeById.values()) {
        if (n.id === currentId) continue;
        const dx = n.x + (n.w ?? DEFAULT_W) / 2 - cx;
        const dy = n.y + (n.h ?? DEFAULT_H) / 2 - cy;
        let inDir = false;
        let along = 0;
        let off = 0;
        if (dir === "right") { inDir = dx > 1; along = dx; off = Math.abs(dy); }
        else if (dir === "left") { inDir = dx < -1; along = -dx; off = Math.abs(dy); }
        else if (dir === "up") { inDir = dy > 1; along = dy; off = Math.abs(dx); }
        else { inDir = dy < -1; along = -dy; off = Math.abs(dx); }
        if (!inDir) continue;
        // Heavily penalize off-axis distance so arrows follow rows/columns: a
        // directly-aligned op wins over a nearer diagonal one.
        const score = along + off * 4;
        if (score < bestScore) { bestScore = score; best = n; }
      }
      if (!best) return false;
      applySelection(best.id);
      return true;
    },
    [nodeById, applySelection]
  );

  useEffect(() => {
    if (!navigable) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      const target = e.target as HTMLElement | null;
      if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) {
        return;
      }
      const dir =
        e.key === "ArrowRight" ? "right"
        : e.key === "ArrowLeft" ? "left"
        : e.key === "ArrowUp" ? "up"
        : e.key === "ArrowDown" ? "down"
        : null;
      if (!dir) return;
      if (arrowSelect(dir)) e.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [navigable, arrowSelect]);
  // Re-fit the view whenever the displayed network changes (entering/leaving a
  // COMP), since React Flow only auto-fits on the initial mount.
  useEffect(() => {
    if (!navigable) return;
    const id = window.setTimeout(() => {
      rfRef.current?.fitView({ padding: fitPadding, duration: fitViewDuration(300) });
    }, 60);
    return () => window.clearTimeout(id);
  }, [navigable, path]);

  // The selected op no longer exists after a level change -- clear it (and tell
  // the panel) whenever the path changes. Also fires on mount, seeding the panel
  // with its empty/placeholder state.
  useEffect(() => {
    if (!navigable) return;
    setSelectedId(null);
    emitSelect(null);
  }, [navigable, path, emitSelect]);

  // Fullscreen modal: a single control opens the graph as a full-viewport
  // overlay (the same ReactFlow, resized via CSS), with Escape / a close button
  // to exit. Replaces the default zoom +/- buttons.
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [fullscreen]);
  // Re-fit after the container resizes (entering/leaving fullscreen).
  useEffect(() => {
    const id = window.setTimeout(() => {
      rfRef.current?.fitView({ padding: fitPadding, duration: fitViewDuration(220) });
    }, 60);
    return () => window.clearTimeout(id);
  }, [fullscreen]);

  const expandIcon = (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3m13-5v3a2 2 0 0 1-2 2h-3" />
    </svg>
  );
  const closeIcon = (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );

  return (
    <div
      className={["tdn-viewer", fullscreen ? "is-fullscreen" : "", className].filter(Boolean).join(" ")}
      style={style}
      onDoubleClick={handleDoubleClick}
    >
      {navigable && (
        <nav
          className="tdn-viewer__breadcrumb"
          aria-label="network path"
          title="Network path - where you are in the network. Double-click a COMP to enter it; click a level here to climb back out."
        >
          <button
            type="button"
            className="tdn-crumb"
            onClick={() => setPath([])}
            disabled={path.length === 0}
            title={path.length === 0 ? "Network root" : "Back to root"}
          >
            {rootLabel || "root"}
          </button>
          {path.map((segment, index) => (
            <span className="tdn-crumb-group" key={`${segment}-${index}`}>
              <span className="tdn-crumb__sep" aria-hidden="true">/</span>
              <button
                type="button"
                className="tdn-crumb"
                aria-current={index === path.length - 1 ? "page" : undefined}
                onClick={() => setPath(path.slice(0, index + 1))}
              >
                {segment}
              </button>
            </span>
          ))}
        </nav>
      )}
      <ReactFlow
        nodes={selectedNodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        proOptions={{ hideAttribution: true }}
        onInit={(instance) => {
          rfRef.current = instance;
        }}
        onNodeClick={navigable ? handleNodeClick : undefined}
        onNodeDoubleClick={navigable ? handleNodeDoubleClick : undefined}
        onPaneClick={navigable ? handlePaneClick : undefined}
        fitView
        fitViewOptions={{ padding: fitPadding }}
        minZoom={0.12}
        maxZoom={1.8}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        selectNodesOnDrag={false}
        panOnDrag
        panOnScroll
        zoomOnScroll
        zoomOnPinch
        zoomOnDoubleClick={false}
        onlyRenderVisibleElements
        preventScrolling
      >
        {/* A single, very faint line grid -- subtle/minimalist, and (being a
            React Flow Background) it pans AND zooms with the network like TD. */}
        <Background
          variant={BackgroundVariant.Lines}
          gap={32}
          lineWidth={1}
          color="rgba(200, 208, 201, 0.035)"
        />
        {!fullscreen && (
          <Controls position="top-right" showZoom={false} showFitView={false} showInteractive={false}>
            <ControlButton onClick={() => setFullscreen(true)} title="View fullscreen" aria-label="View fullscreen">
              {expandIcon}
            </ControlButton>
          </Controls>
        )}
      </ReactFlow>
      {fullscreen && (
        <button
          type="button"
          className="tdn-viewer__close"
          onClick={() => setFullscreen(false)}
          aria-label="Close fullscreen"
        >
          {closeIcon}
        </button>
      )}
    </div>
  );
}

function OperatorTile({ data }: NodeProps<OperatorNode>) {
  const inputHandles = Array.from({ length: Math.max(data.inputCount, 1) }, (_, index) => index);
  const compHandles = Array.from({ length: data.compInputCount }, (_, index) => index);

  const className = [
    "tdn-operator",
    data.canEnter ? "tdn-operator--enterable" : "",
    data.selected ? "tdn-operator--selected" : ""
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={className} style={{ "--family-color": data.familyColor } as CSSProperties}>
      {inputHandles.map((index) => (
        <Handle
          className="tdn-handle tdn-handle--target"
          id={`in-${index}`}
          key={`in-${index}`}
          position={Position.Left}
          style={{ top: `${handlePercent(index, inputHandles.length)}%` }}
          type="target"
        />
      ))}
      {compHandles.map((index) => (
        <Handle
          className="tdn-handle tdn-handle--target tdn-handle--comp"
          id={`comp-in-${index}`}
          key={`comp-in-${index}`}
          position={Position.Bottom}
          style={{ left: `${handlePercent(index, compHandles.length)}%` }}
          type="target"
        />
      ))}
      <Handle
        className="tdn-handle tdn-handle--source"
        id="out"
        position={Position.Right}
        type="source"
      />
      {data.isDockHost && (
        <Handle
          className="tdn-handle tdn-handle--dock"
          id="dock-out"
          position={Position.Bottom}
          style={{ left: "50%" }}
          type="source"
        />
      )}
      {data.isDocked && (
        <Handle
          className="tdn-handle tdn-handle--dock"
          id="dock-in"
          position={Position.Top}
          style={{ left: "50%" }}
          type="target"
        />
      )}
      {data.isRefSource && (
        <Handle
          className="tdn-handle tdn-handle--ref"
          id="ref-out"
          position={Position.Top}
          style={{ left: "62%" }}
          type="source"
        />
      )}
      {data.isRefTarget && (
        <Handle
          className="tdn-handle tdn-handle--ref"
          id="ref-in"
          position={Position.Top}
          style={{ left: "38%" }}
          type="target"
        />
      )}
      <div className="tdn-operator__head" />
      <div className="tdn-operator__body">
        <div className="tdn-operator__name" title={data.name}>
          {data.name}
        </div>
        <div className="tdn-operator__meta">
          {/* Family is conveyed by the head-bar colour, so the type alone is
              enough here -- no redundant family chip. */}
          <span>{data.type}</span>
        </div>
      </div>
    </div>
  );
}

function AnnotationBox({ data }: NodeProps<AnnotationNode>) {
  return (
    <div
      className="tdn-annotation"
      style={
        {
          "--annotation-color": data.color,
          width: data.width,
          height: data.height
        } as CSSProperties
      }
    >
      <span>{data.title}</span>
      {data.text ? <p>{data.text}</p> : null}
    </div>
  );
}

function toFlowElements(
  graph: NormalizedGraph,
  showAnnotations: boolean
): { nodes: TdnFlowNode[]; edges: Edge[] } {
  const inputCounts = new Map<string, number>();
  const compInputCounts = new Map<string, number>();
  const refSources = new Set<string>();
  const refTargets = new Set<string>();

  for (const edge of graph.edges) {
    if (edge.ref) {
      // Ref edges connect via dedicated top handles, not input slots, so they
      // must NOT inflate a node's input-handle count.
      refSources.add(edge.from);
      refTargets.add(edge.to);
      continue;
    }
    const counts = edge.comp ? compInputCounts : inputCounts;
    counts.set(edge.to, Math.max(counts.get(edge.to) ?? 0, edge.inputIndex + 1));
  }

  // Dock relationships -- used now only for the dock TETHER edges + the dock
  // handle flags. Docked ops keep their REAL TDN positions (no re-layout), so
  // they sit exactly where TD placed them (typically a row under their host,
  // inside the host's annotation) for a faithful 1:1 view.
  const byId = new Map<string, GraphNode>(graph.nodes.map((n) => [n.id, n]));
  const dockedByHost = new Map<string, GraphNode[]>();
  const dockedIds = new Set<string>();
  for (const node of graph.nodes) {
    if (node.dock && byId.has(node.dock) && node.dock !== node.id) {
      const list = dockedByHost.get(node.dock) ?? [];
      list.push(node);
      dockedByHost.set(node.dock, list);
      dockedIds.add(node.id);
    }
  }

  // Every node at its REAL TD position + size. TD positions are the BOTTOM-LEFT
  // corner with Y up; React Flow positions are the TOP-LEFT with Y down, so the
  // top-left Y is -(y + height). Annotations convert the same way (see
  // annotationToNode), so operators land exactly inside their annotation COMP --
  // the 1:1 layout TD itself shows.
  const nodes: TdnFlowNode[] = graph.nodes.map((node) => {
    const w = node.w ?? DEFAULT_W;
    const h = node.h ?? DEFAULT_H;
    return {
      id: node.id,
      type: "operator",
      position: { x: node.x, y: -(node.y + h) },
      style: { width: w, height: h },
      data: {
        name: node.name,
        type: node.type,
        family: node.family,
        familyColor: FAMILY_COLORS[node.family] ?? FAMILY_COLORS.OBJECT ?? "#b9b09d",
        inputCount: inputCounts.get(node.id) ?? 0,
        compInputCount: compInputCounts.get(node.id) ?? 0,
        isDockHost: dockedByHost.has(node.id),
        isDocked: dockedIds.has(node.id),
        isRefSource: refSources.has(node.id),
        isRefTarget: refTargets.has(node.id),
        // childCount is only set by the single-level parse, so canEnter is
        // naturally false in the flattened (non-navigable) view.
        canEnter: (node.childCount ?? 0) > 0,
        // Set per-render by the selectedNodes memo in TdnViewer.
        selected: false
      },
      draggable: false,
      selectable: false
    };
  });

  const edges: Edge[] = graph.edges.map((edge, index) => {
    // Parameter reference (e.g. a Feedback TOP's Target, a Render TOP's camera):
    // a real dependency but NOT a data wire -- draw it dotted + muted with a
    // small arrow, so it reads like the docked-DAT tethers, not a signal flow.
    if (edge.ref) {
      return {
        id: `ref:${edge.from}->${edge.to}:${edge.inputIndex}:${index}`,
        source: edge.from,
        target: edge.to,
        // Top handles + smoothstep with a tall offset -> the dotted link rises
        // well ABOVE the tiles and drops into each node with a clear vertical
        // segment, reading as a feedback loop instead of hugging the top edge.
        sourceHandle: "ref-out",
        targetHandle: "ref-in",
        type: "smoothstep",
        pathOptions: { offset: 30, borderRadius: 10 },
        focusable: false,
        selectable: false,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 11,
          height: 11,
          color: "rgba(150, 162, 154, 0.85)"
        },
        style: {
          stroke: "rgba(150, 162, 154, 0.8)",
          strokeWidth: 1.4,
          strokeDasharray: "2 4"
        }
      };
    }
    return {
      id: `${edge.from}->${edge.to}:${edge.comp ? "comp" : "in"}:${edge.inputIndex}:${index}`,
      source: edge.from,
      target: edge.to,
      sourceHandle: "out",
      targetHandle: edge.comp ? `comp-in-${edge.inputIndex}` : `in-${edge.inputIndex}`,
      type: "smoothstep",
      animated: edge.comp,
      focusable: false,
      selectable: false,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 14,
        height: 14,
        color: edge.comp ? "#cda05a" : "#6ee668"
      },
      style: {
        stroke: edge.comp ? "rgba(205, 160, 90, 0.74)" : "rgba(110, 230, 104, 0.62)",
        strokeWidth: edge.comp ? 1.8 : 1.5
      }
    };
  });

  // Dock tethers: a muted, dashed link from the host's bottom to each docked op,
  // visually distinct from data-flow wires (no arrowhead, no animation) so it
  // reads as "attached to", not "feeds into".
  for (const [hostId, list] of dockedByHost) {
    for (const node of list) {
      edges.push({
        id: `dock:${hostId}->${node.id}`,
        source: hostId,
        target: node.id,
        sourceHandle: "dock-out",
        targetHandle: "dock-in",
        // Straight tether (host bottom -> docked top): no orthogonal jog.
        type: "straight",
        focusable: false,
        selectable: false,
        style: {
          stroke: "rgba(150, 162, 154, 0.5)",
          strokeWidth: 1.2,
          strokeDasharray: "4 4"
        }
      });
    }
  }

  if (showAnnotations) {
    for (const annotation of graph.annotations) {
      nodes.push(annotationToNode(annotation, nodes.length));
    }
  }

  return { nodes, edges };
}

function annotationToNode(annotation: GraphAnnotation, index: number): AnnotationNode {
  return {
    id: `annotation-${index}`,
    type: "annotation",
    position: {
      x: annotation.x,
      y: -annotation.y - annotation.h
    },
    data: {
      title: annotation.title ?? "annotation",
      text: annotation.text ?? "",
      width: annotation.w || 280,
      height: annotation.h || 160,
      // Full-opacity color; the CSS color-mixes it against the viewer's line /
      // panel colors so the container stays visible even when the authored color
      // is dark (which, at low alpha, vanished against the dark canvas).
      color: annotation.color ? rgbToCss(annotation.color, 1) : "rgb(150, 160, 152)"
    },
    draggable: false,
    selectable: false,
    zIndex: -1
  };
}

function rgbToCss(rgb: RGB, alpha: number): string {
  return `rgba(${channel(rgb[0])}, ${channel(rgb[1])}, ${channel(rgb[2])}, ${alpha})`;
}

function channel(value: number): number {
  return Math.round(Math.max(0, Math.min(1, value)) * 255);
}

function handlePercent(index: number, count: number): number {
  if (count <= 1) return 50;
  return 20 + (index * 60) / (count - 1);
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
