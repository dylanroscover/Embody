import "@xyflow/react/dist/style.css";
import "./tdnViewer.css";

import {
  Background,
  BackgroundVariant,
  ControlButton,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
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
};

// Tile footprint used for the docked-row layout (matches tdnViewer.css).
const TILE_W = 168;
const TILE_H = 84;
const DOCK_VGAP = 46; // gap below the host to the docked row
const DOCK_HGAP = 36; // gap between docked tiles in the row

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

const FAMILY_COLORS: Record<string, string> = {
  TOP: "#6ee668",
  CHOP: "#9ccb5a",
  SOP: "#c9954f",
  DAT: "#d98a6a",
  MAT: "#b291b0",
  POP: "#d9c25a",
  COMP: "#5fa777",
  OBJECT: "#b9b09d"
};

const NODE_TYPES: NodeTypes = {
  annotation: memo(AnnotationBox),
  operator: memo(OperatorTile)
};

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
          <Controls showZoom={false} showFitView={false} showInteractive={false}>
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

  for (const edge of graph.edges) {
    const counts = edge.comp ? compInputCounts : inputCounts;
    counts.set(edge.to, Math.max(counts.get(edge.to) ?? 0, edge.inputIndex + 1));
  }

  // Dock relationships: group docked ops under a host that actually exists in the
  // graph. Their raw TD coords pack them tight enough to overlap at tile size, so
  // we re-lay them in a tidy, evenly-spaced row centred under the host -- which
  // both removes the overlap and reads as "these belong to that host".
  const byId = new Map<string, GraphNode>(graph.nodes.map((n) => [n.id, n]));
  const dockedByHost = new Map<string, GraphNode[]>();
  for (const node of graph.nodes) {
    if (node.dock && byId.has(node.dock) && node.dock !== node.id) {
      const list = dockedByHost.get(node.dock) ?? [];
      list.push(node);
      dockedByHost.set(node.dock, list);
    }
  }

  const dockedPos = new Map<string, { x: number; y: number }>();
  const dockedIds = new Set<string>();
  for (const [hostId, list] of dockedByHost) {
    const host = byId.get(hostId)!;
    const ordered = [...list].sort((a, b) => a.x - b.x || a.id.localeCompare(b.id));
    const step = TILE_W + DOCK_HGAP;
    const rowWidth = (ordered.length - 1) * step;
    const hostCenter = host.x + TILE_W / 2;
    const rowTop = -host.y + TILE_H + DOCK_VGAP;
    ordered.forEach((node, i) => {
      dockedIds.add(node.id);
      dockedPos.set(node.id, {
        x: hostCenter - rowWidth / 2 - TILE_W / 2 + i * step,
        y: rowTop
      });
    });
  }

  const nodes: TdnFlowNode[] = graph.nodes.map((node) => ({
    id: node.id,
    type: "operator",
    position: dockedPos.get(node.id) ?? {
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
      isDocked: dockedIds.has(node.id)
    },
    draggable: false,
    selectable: false
  }));

  const edges: Edge[] = graph.edges.map((edge, index) => ({
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
  }));

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
