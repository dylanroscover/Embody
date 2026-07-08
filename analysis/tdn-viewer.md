# TDN Viewer — How It Works

**Date:** 2026-07-08
**Project:** Embody — TouchDesigner externalization & AI-assisted development

---

## Overview

The TDN viewer is a **web-based React + React Flow component**, shipped as the private package `@embody/tdn-viewer`. It renders `.tdn` YAML files as faithful 1:1 TouchDesigner-style node graphs in the browser. There is **no in-TouchDesigner TDN viewer COMP** — inside TD, `.tdn` is imported as a live network and TD's own network editor is the view.

---

## Primary Component — `platform/packages/tdn-viewer/`

```
src/
├── TdnViewer.tsx     # Main component (915 lines) — React Flow canvas
├── parseTDN.ts      # TDN dict → NormalizedGraph (349 lines)
├── tdnViewer.css    # Styling (329 lines)
└── index.ts          # Public exports
```

- Built on **`@xyflow/react` v12** (React Flow), React 19
- Graph types shared via **`@embody/contracts`** (frozen contract C6 — the normalized graph the tdn-viewer renders)

### Key facts from `package.json`
- Private package: `@embody/tdn-viewer`
- Described as *"Inert TDN node graph viewer for embody.tools."*

---

## Two Render Modes

| Mode | Prop | How It Works | Used Where |
|------|------|--------------|------------|
| **Flattened** | `navigable=false` (default) | `parseTDN(tdn)` recurses into every nested COMP, splays all descendants onto one plane | Homepage hero background, Collection card-cover thumbnails |
| **Navigable** | `navigable=true` | `parseTDNLevel(tdn, path)` parses one level at a time; double-click a COMP to descend; breadcrumb address bar to climb back out | Specimen detail page `/c/[slug]` |

Faithful layout: nodes placed at their **real TDN coordinates** (TD's Y-up → React Flow Y-down conversion: `y: -(node.y + h)`). No layout engine needed — *"TDN already carries absolute positions + input-index connections, so this is pure draw-from-data."*

---

## Component Props

```tsx
export interface TdnViewerProps {
  tdn: Record<string, unknown>;
  className?: string;
  height?: number | string;
  navigable?: boolean;          // single-click select + double-click enter COMP
  rootLabel?: string;           // label for the root crumb
  onSelect?: (selection: TdnSelection | null) => void;
  fitPadding?: PaddingValue | PaddingObject;
  showAnnotations?: boolean;    // draw annotateCOMP boxes (default true)
}
```

---

## Operator Tiles

- Placed at real TDN coordinates with real sizes (1:1 with TD's network editor)
- TD's Y-up converted to React Flow's Y-down: `y: -(node.y + h)`
- Family colors map TD's own family palette:

```ts
const FAMILY_COLORS: Record<string, string> = {
  TOP: "#9d8cdb",   // purple
  CHOP: "#7cc45a",   // green
  SOP: "#5fa3dd",    // blue
  POP: "#7a78ec",
  MAT: "#c9b85f",
  DAT: "#c684ad",    // mauve
  COMP: "#9aa1a8",   // grey
  OBJECT: "#b9b09d"
};
```

Operators land exactly inside their annotation COMP boxes — the same layout TD itself shows.

---

## Edge Rendering

| Edge Type | Style | Notes |
|-----------|-------|-------|
| Data wires | Smoothstep edges | Standard left/right operator connections |
| COMP-input connectors | Animated amber | Top/bottom connectors |
| Docked-op tethers | Dashed grey, no arrowheads | "Attached to" not "feeds into" |
| Parameter-reference edges | Dotted muted links | Render TOP's camera, Feedback TOP's target — drawn via top handles so they arc above data wires |

---

## Selection → Properties Panel

Selection surfaces via both an `onSelect` callback **and** a DOM `CustomEvent`, so the Astro host page (which can't pass a function prop into the React island) can render a properties panel:

```tsx
const emitSelect = useCallback(
  (selection: TdnSelection | null) => {
    onSelect?.(selection);
    if (typeof document !== "undefined") {
      document.dispatchEvent(new CustomEvent("tdnviewer:select", { detail: selection }));
    }
  },
  [onSelect]
);
```

The host page listens:

```ts
document.addEventListener("tdnviewer:select", (e) => { ... });
```

### `TdnSelection` payload

Carries everything the properties panel needs:

| Field | Description |
|-------|-------------|
| `path` | Full operator path |
| `id`, `name`, `type` | Identity |
| `family` | TOP/CHOP/SOP/DAT/COMP/etc. |
| `isComp` | Whether this is a COMP (can drill down) |
| `childCount` | Number of children (for drill-down indicator) |
| `comment` | Node comment text |
| `tags` | Operator tags |
| `parameters` | Customized built-in parameters |
| `custom_pars` | Custom parameter pages |
| `datContent` | Normalized DAT content (shader GLSL / script Python / table text) |

---

## The Parser — `parseTDN.ts`

Exports: `parseTDN`, `parseTDNLevel`, `getNetworkAtPath`, `operatorsAtLevel`

Responsibilities:
1. Walks the TDN dict (handles `operators` at root and `children` inside nested COMPs)
2. Resolves input paths (absolute `/`, relative, or sibling-name)
3. Derives the family from the type suffix (e.g., `noiseTOP` → `TOP`)
4. Hoists `type_defaults` sizes for a 1:1 footprint
5. Collects data wires + docked tethers + param-reference edges
6. Emits annotation boxes

### The Frozen Graph Contract — `platform/packages/contracts/graph.ts`

Marked `FROZEN CONTRACT C6` — defines `GraphNode`, `GraphEdge`, `GraphAnnotation`, `NormalizedGraph`. The parser needs no layout engine — TDN already carries absolute positions + input-index connections.

---

## Where the Viewer Is Used

| Consumer | Location | Mode |
|----------|----------|------|
| **Homepage hero background** | `platform/apps/web/src/pages/index.astro` | Flattened, no annotations, decorative |
| **Collection grid covers** | `platform/apps/web/src/components/SpecimenCoverGraph.tsx` | Flattened, lazy-mounted (`client:visible`) |
| **Specimen detail page** | `platform/apps/web/src/pages/c/[slug].astro:609` | Navigable + properties panel |
| **Specimen graph API** | `platform/apps/web/src/pages/api/specimens/[slug]/tdn.ts` | Returns pre-trimmed JSON graph (~few KB) |

### Specimen cover thumbnails (`SpecimenCoverGraph.tsx`)

Lazy-mounted with Astro `client:visible`. Fetches graph from `/api/specimens/<slug>/tdn?format=graph` on mount, or uses SSR-provided `tdn` for first-page fast-path:

```tsx
<TdnViewer tdn={state.tdn} height="100%" />
```

### Homepage hero (`index.astro`)

```astro
<TdnViewer tdn={sampleTdn} height="100%" showAnnotations={false} client:only="react" />
```

### Specimen detail page (`c/[slug].astro`)

Renders navigable `TdnViewer` at line 609 with a properties panel (`aside.specimen-app__props`, line 695) fed by the `tdnviewer:select` CustomEvent at line 1048.

---

## Secondary: Raw YAML Source Viewer

### `platform/apps/web/src/components/TdnYamlViewer.tsx` (455 lines)

Read-only, TDN-aware YAML viewer for the specimen "raw TDN" block. Features:
- Syntax highlighting (with TDN `=expression` shorthand in brand accent)
- Indentation-based collapsible sections
- In-place search
- Line numbers
- Word-wrap toggle
- Expand/collapse-all
- Jump-to-section chips
- Caps rendering at 5000 lines with truncation notice
- Keyboard-navigable "jump to operator/annotation" dropdown

Tokenizes via companion `src/lib/tdnTokenize.ts`.

### Editable variant

```
platform/apps/web/src/components/TdnYamlEditor.tsx
```

The editable variant for the contribute/edit flow.

### Styling

CSS for both in `platform/apps/web/src/styles/embody.css` under `.tdn-yaml__*` and `.tdn-editor__*` selectors.

---

## Specimens — Gallery TDN Networks

```
specimens/
├── manifest.json
├── manifest.schema.json
├── compositing/kaleidoscope.tdn
├── generative/{reaction-diffusion,plasma-interference}.tdn
├── raymarching-sdf/mandelbulb-march.tdn
├── simulation/murmuration.tdn
└── 3d/noise-terrain.tdn
```

Per `docs/platform/collection.md`:
> Each card flips between a **rendered result** and the **live node graph** (an in-browser TDN viewer), so you can see both what it makes and how it's wired.

A Specimen card flips between:
- The rendered JPG/cover-video (`platform/apps/web/public/specimens/*.jpg`)
- A `TdnViewer` (flattened cover thumbnail via `SpecimenCoverGraph.tsx`)

On the detail page (`/c/[slug]`), the user gets:
- The **navigable** `TdnViewer` (drill-down + properties panel + rendered result thumbnail sidebar)
- The **raw YAML** `TdnYamlViewer`

TDN is fetched from `/api/specimens/<slug>/tdn` (graph format) and raw `.tdn` text via `getParsedTdnForSlug` in `src/server/tdn.ts`.

---

## E2E Test

```
platform/apps/web/e2e/viewer-drilldown.spec.ts
```

Tests the navigable viewer's drill-down/up behavior on `/c/noise-terrain` — verifies breadcrumb gain/loss when descending/ascending a COMP, and that the child operator set differs from the root set.

---

## No In-TouchDesigner TDN Viewer

There is **no in-TD TDN visualization COMP**. Inside TouchDesigner:
1. `read_tdn` MCP tool reads live state as a TDN dict (no disk I/O)
2. `import_network` MCP tool rebuilds the TDN dict as a **real live network** in a target COMP (9-phase best-effort importer)
3. TD's own network editor IS the visualization

The `opviewerCOMP` hits in `.tdn` files (e.g., `help.tdn`) are TD's native **Operator Viewer COMP** used for help/theming text displays inside the Embody toolbar — not a TDN renderer.

---

## Quick-Reference File Map

### Primary TDN graph viewer (React + React Flow)

| File                                             | Purpose                                       |
|--------------------------------------------------|-----------------------------------------------|
| `platform/packages/tdn-viewer/src/TdnViewer.tsx` | Main component (915 lines)                    |
| `platform/packages/tdn-viewer/src/parseTDN.ts`   | TDN dict → NormalizedGraph parser (349 lines) |
| `platform/packages/tdn-viewer/src/tdnViewer.css` | Styling (329 lines)                           |
| `platform/packages/tdn-viewer/src/index.ts`      | Public exports                                |
| `platform/packages/tdn-viewer/package.json`      | Package config                                |
| `platform/packages/contracts/graph.ts`           | Frozen graph contract (C6)                    |

### Host pages / consumers

| File                                                             | Purpose                            |
|------------------------------------------------------------------|------------------------------------|
| `platform/apps/web/src/pages/c/[slug].astro`                     | Specimen detail — navigable viewer |
| `platform/apps/web/src/pages/index.astro`                        | Homepage hero — flattened          |
| `platform/apps/web/src/pages/collection/index.astro`             | Collection grid — flattened covers |
| `platform/apps/web/src/components/SpecimenCoverGraph.tsx`        | Lazy card-cover graph              |
| `platform/apps/web/src/components/SpecimenCover.astro`           | Cover wrapper with NETWORK layer   |
| `platform/apps/web/src/pages/api/specimens/[slug]/tdn.ts`        | Graph API endpoint                 |
| `platform/apps/web/src/fixtures/{sample-tdn,specimen-graphs}.ts` | SSR fast-path fixtures             |
| `platform/apps/web/e2e/viewer-drilldown.spec.ts`                 | E2E test                           |

### Secondary raw-YAML source viewer/editor

| File                                                 | Purpose                                     |
|------------------------------------------------------|---------------------------------------------|
| `platform/apps/web/src/components/TdnYamlViewer.tsx` | Read-only YAML viewer (455 lines)           |
| `platform/apps/web/src/components/TdnYamlEditor.tsx` | Editable YAML editor                        |
| `platform/apps/web/src/lib/tdnTokenize.ts`           | YAML tokenizer                              |
| `platform/apps/web/src/lib/tdnEnvelope.ts`           | TDN envelope utilities                      |
| `platform/apps/web/src/styles/embody.css`            | `.tdn-yaml__*` / `.tdn-editor__*` selectors |

### In-TD import (NOT a viewer — the live-network path)

| File                              | Purpose                                       |
|-----------------------------------|-----------------------------------------------|
| `dev/embody/Embody/EnvoyExt.py`   | `read_tdn` / `import_network` MCP wiring      |
| `dev/embody/Embody/envoy_read.py` | `read_tdn` implementation                     |
| `dev/embody/Embody/TDNExt.py`     | TDN export/import engine                      |
| `docs/embody/manager-ui.md`       | Manager UI buttons (Import TDN / Export COMP) |

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    embody.tools (Astro site)                │
│                                                             │
│  ┌────────────────┐ ┌──────────────────────────┐            │
│  │ /api/specimens/│ │  /c/[slug] (detail page) │            │
│  │  [slug]/tdn.ts │ │                          │            │
│  │  (graph JSON)  │ │  ┌─────────────────────┐ │            │
│  └──────┬─────────┘ │  │  TdnViewer          │ │            │
│         │           │  │  (navigable mode)   │ │            │
│         │           │  │  @embody/tdn-viewer │ │            │
│         │ merged ───┼─▶│                     │ │            │
│         │           │  │  React Flow canvas  │ │            │
│         │           │  │  annotated          │ │            │
│         │           │  │  drilled COMPs      │ │            │
│         │           │  │  breadcrumb nav     │ │            │
│         │           │  └────────┬────────────┘ │            │ 
│         │           │           │ onSelect     │            │
│         │           │           ▼              │            │
│         │           │  ┌─────────────────────┐ │            │ 
│         │           │  │ Properties Panel    │ │            │
│         │           │  │ (TdnSelection data) │ │            │
│         │           │  └─────────────────────┘ │            │ 
│         │           │                          │            │
│         │           │  ┌─────────────────────┐ │            │ 
│         │           ├──│ TdnYamlViewer       │ │            │
│         │           │  │ (read-only source)  │ │            │
│         │           │  └─────────────────────┘ │            │   
│         │           └──────────────────────────┘            │      
│         │                                                   │
└─────────┼──────────────────────────────────────────├─────────
          │
          │ /api/specimens/[slug]/tdn?format=graph
          │ (pre-trimmed JSON, ~few KB)
          ▼
┌──────────────────────────────────────────────────────┐
│ specimens/<category>/<name>.tdn (YAML files on disk) │
└──────────────────────────────────────────────────────┘
```
