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
import type { CSSProperties } from "react";
import type { GraphAnnotation, GraphNode, NormalizedGraph, RGB } from "@embody/contracts";
import { parseTDN } from "./parseTDN";

export interface TdnViewerProps {
  tdn: Record<string, unknown>;
  className?: string;
  height?: number | string;
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
};

// Tile footprint used for the docked-row layout (matches tdnViewer.css).
const TILE_W = 168;
const TILE_H = 84;
const DOCK_VGAP = 46; // gap below the host to the docked row
const DOCK_HGAP = 36; // gap between docked tiles in the row
const NODE_GAP = 24; // minimum gap kept between main tiles when nudging overlaps

// Nudge any overlapping main-node tiles apart in place. The viewer draws a fixed
// TILE_W x TILE_H box per op, often larger than TD's own node spacing, so tightly
// packed networks collide. This is a minimal relaxation: only nodes that actually
// overlap move, along the axis of least penetration, leaving the rest of the
// authored layout intact. Deterministic (stable id order); a few passes converge.
function resolveOverlaps(
  pos: Map<string, { x: number; y: number }>,
  w: number,
  h: number,
  gap: number
): void {
  const ids = [...pos.keys()].sort();
  const minDx = w + gap;
  const minDy = h + gap;
  const MAX_ITERS = 24;
  for (let iter = 0; iter < MAX_ITERS; iter++) {
    let moved = false;
    for (let i = 0; i < ids.length; i++) {
      const a = pos.get(ids[i]!)!;
      for (let j = i + 1; j < ids.length; j++) {
        const b = pos.get(ids[j]!)!;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const penX = minDx - Math.abs(dx);
        const penY = minDy - Math.abs(dy);
        if (penX > 0 && penY > 0) {
          if (penX <= penY) {
            const push = (penX / 2) * (dx < 0 ? -1 : 1);
            a.x -= push;
            b.x += push;
          } else {
            const push = (penY / 2) * (dy < 0 ? -1 : 1);
            a.y -= push;
            b.y += push;
          }
          moved = true;
        }
      }
    }
    if (!moved) break;
  }
}

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

export function TdnViewer({ tdn, className, height = 520 }: TdnViewerProps) {
  const graph = useMemo(() => parseTDN(tdn), [tdn]);
  const { nodes, edges } = useMemo(() => toFlowElements(graph), [graph]);

  const style = useMemo<CSSProperties>(
    () => ({
      height: typeof height === "number" ? `${height}px` : height
    }),
    [height]
  );

  // Double-click anywhere fits the WHOLE graph into view (a reset), instead of
  // React Flow's default zoom-IN -- zoomOnDoubleClick is disabled below so the
  // two don't conflict.
  const rfRef = useRef<ReactFlowInstance<TdnFlowNode, Edge> | null>(null);
  const handleDoubleClick = useCallback(() => {
    rfRef.current?.fitView({ padding: 0.24, duration: fitViewDuration(320) });
  }, []);

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
      rfRef.current?.fitView({ padding: 0.24, duration: fitViewDuration(220) });
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
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        proOptions={{ hideAttribution: true }}
        onInit={(instance) => {
          rfRef.current = instance;
        }}
        fitView
        fitViewOptions={{ padding: 0.24 }}
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
        <Background
          color="rgba(200, 208, 201, 0.10)"
          gap={32}
          size={1}
          variant={BackgroundVariant.Dots}
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

  return (
    <div className="tdn-operator" style={{ "--family-color": data.familyColor } as CSSProperties}>
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

function toFlowElements(graph: NormalizedGraph): { nodes: TdnFlowNode[]; edges: Edge[] } {
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

  // Dock relationships: group docked ops under a host that actually exists in the
  // graph, so we can re-lay them in a tidy row under that host.
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

  // Main (non-docked) tiles at raw TD coords, then nudge overlaps apart so no two
  // main tiles collide. Docks are placed afterwards relative to the RESOLVED host
  // position, so they ride along with any host the nudge moved.
  const mainPos = new Map<string, { x: number; y: number }>();
  for (const node of graph.nodes) {
    if (!dockedIds.has(node.id)) mainPos.set(node.id, { x: node.x, y: -node.y });
  }
  resolveOverlaps(mainPos, TILE_W, TILE_H, NODE_GAP);

  const dockedPos = new Map<string, { x: number; y: number }>();
  for (const [hostId, list] of dockedByHost) {
    const host = mainPos.get(hostId);
    if (!host) continue; // host is itself docked (degenerate) -- skip re-layout
    const ordered = [...list].sort((a, b) => a.x - b.x || a.id.localeCompare(b.id));
    const step = TILE_W + DOCK_HGAP;
    const rowWidth = (ordered.length - 1) * step;
    const hostCenter = host.x + TILE_W / 2;
    const rowTop = host.y + TILE_H + DOCK_VGAP;
    ordered.forEach((node, i) => {
      dockedPos.set(node.id, {
        x: hostCenter - rowWidth / 2 - TILE_W / 2 + i * step,
        y: rowTop
      });
    });
  }

  const nodes: TdnFlowNode[] = graph.nodes.map((node) => ({
    id: node.id,
    type: "operator",
    position: dockedPos.get(node.id) ?? mainPos.get(node.id) ?? {
      x: node.x,
      y: -node.y
    },
    data: {
      name: node.name,
      type: node.type,
      family: node.family,
      familyColor: FAMILY_COLORS[node.family] ?? FAMILY_COLORS.OBJECT,
      inputCount: inputCounts.get(node.id) ?? 0,
      compInputCount: compInputCounts.get(node.id) ?? 0,
      isDockHost: dockedByHost.has(node.id),
      isDocked: dockedIds.has(node.id),
      isRefSource: refSources.has(node.id),
      isRefTarget: refTargets.has(node.id)
    },
    draggable: false,
    selectable: false
  }));

  // Resolved position of every operator tile (annotations are pushed later and
  // never obstruct wires, so they're excluded).
  const finalPos = new Map<string, { x: number; y: number }>(
    nodes.map((n) => [n.id, n.position])
  );

  // A data wire that runs straight across an intervening same-row tile reads
  // poorly. If another MAIN tile sits in the wire's horizontal span at its row,
  // return a raised centreY so the wire arcs just above the highest such tile.
  const LIFT_CLEARANCE = 28;
  const liftFor = (from: string, to: string): number | undefined => {
    const s = finalPos.get(from);
    const t = finalPos.get(to);
    if (!s || !t) return undefined;
    const sy = s.y + TILE_H / 2;
    const ty = t.y + TILE_H / 2;
    if (Math.abs(sy - ty) > TILE_H) return undefined; // not the same row
    const xMin = Math.min(s.x + TILE_W, t.x);
    const xMax = Math.max(s.x + TILE_W, t.x);
    if (xMax - xMin < TILE_W * 0.4) return undefined; // adjacent -> nothing between
    const edgeY = (sy + ty) / 2;
    let top = Infinity;
    for (const [id, p] of mainPos) {
      if (id === from || id === to) continue;
      if (p.x + TILE_W > xMin && p.x < xMax && edgeY >= p.y && edgeY <= p.y + TILE_H) {
        top = Math.min(top, p.y);
      }
    }
    return top === Infinity ? undefined : top - LIFT_CLEARANCE;
  };

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
    const liftY = edge.comp ? undefined : liftFor(edge.from, edge.to);
    return {
      id: `${edge.from}->${edge.to}:${edge.comp ? "comp" : "in"}:${edge.inputIndex}:${index}`,
      source: edge.from,
      target: edge.to,
      sourceHandle: "out",
      targetHandle: edge.comp ? `comp-in-${edge.inputIndex}` : `in-${edge.inputIndex}`,
      // Arc over an intervening same-row tile when one is detected (LiftedEdge).
      type: liftY !== undefined ? "lifted" : "smoothstep",
      ...(liftY !== undefined ? { data: { liftY } } : {}),
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
        type: "smoothstep",
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

  for (const annotation of graph.annotations) {
    nodes.push(annotationToNode(annotation, nodes.length));
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
      color: annotation.color ? rgbToCss(annotation.color, 0.32) : "rgba(200, 208, 201, 0.24)"
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
