import { useEffect, useRef, useState } from "react";
import { TdxnViewer } from "@embody/tdxn-viewer";

// The NETWORK layer of a specimen cover. Lazy-mounted by Astro (client:visible),
// so the React/ReactFlow island only hydrates when the card scrolls into view.
//
// Graph source, in order of preference:
//   1. An SSR-provided `tdxn` (the fast path for the first, server-rendered page
//      -- no fetch, paints immediately).
//   2. Otherwise FETCH /api/specimens/<slug>/tdn?format=graph on mount: a light,
//      pre-trimmed JSON graph. This is how covers scale to thousands -- nothing
//      is bundled at build time; each card pulls only its own graph, only when
//      it scrolls in.
//
// A skeleton shows while fetching. On error the layer stays a quiet skeleton
// (the RESULT layer / flip still work), never a broken viewer.

interface Props {
  slug: string;
  /** Optional SSR graph (first page fast-path). Omit to lazy-fetch. */
  tdxn?: Record<string, unknown>;
}

type State =
  | { status: "ready"; tdxn: Record<string, unknown> }
  | { status: "loading" }
  | { status: "error" };

export default function SpecimenCoverGraph({ slug, tdxn }: Props) {
  const [state, setState] = useState<State>(
    tdxn ? { status: "ready", tdxn } : { status: "loading" }
  );
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    // SSR fast-path already has the graph; no fetch needed.
    if (tdxn) {
      setState({ status: "ready", tdxn });
      return () => {
        mounted.current = false;
      };
    }

    const controller = new AbortController();
    setState({ status: "loading" });

    fetch(`/api/specimens/${encodeURIComponent(slug)}/tdn?format=graph`, {
      signal: controller.signal
    })
      .then((response) => {
        if (!response.ok) throw new Error(`graph fetch failed: ${response.status}`);
        return response.json();
      })
      .then((graph: unknown) => {
        if (!mounted.current) return;
        if (graph && typeof graph === "object" && !Array.isArray(graph)) {
          setState({ status: "ready", tdxn: graph as Record<string, unknown> });
        } else {
          setState({ status: "error" });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || !mounted.current) return;
        setState({ status: "error" });
        void error;
      });

    return () => {
      mounted.current = false;
      controller.abort();
    };
  }, [slug, tdxn]);

  if (state.status === "ready") {
    return <TdxnViewer tdxn={state.tdxn} height="100%" />;
  }

  // Loading and error both show the quiet skeleton; error simply never resolves
  // into a viewer. (The result layer + flip remain fully usable either way.)
  return (
    <div
      className={`cover-skeleton${state.status === "error" ? " is-error" : ""}`}
      aria-hidden="true"
    >
      <span className="cover-skeleton__shimmer" />
    </div>
  );
}
