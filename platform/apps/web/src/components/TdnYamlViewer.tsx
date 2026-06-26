import { useMemo, useState, useRef, useEffect, type CSSProperties, type ReactNode } from "react";

// Read-only, TDN-aware YAML viewer for the specimen "raw TDN" block:
// syntax highlighting (with the TDN =expression shorthand in the brand accent),
// indentation-based collapsible sections, in-place search, line numbers, a
// word-wrap toggle, expand/collapse-all, and jump-to-section chips. SSR renders
// the highlighted lines so the colored YAML shows before hydration; the controls
// come alive on the client.

export type TdnYamlSummary = {
  operators: number;
  connections: number;
  annotations: number;
  lines: number;
};

type Props = { raw: string; summary: TdnYamlSummary };

type Line = {
  n: number; // 1-based line number
  indent: number;
  text: string; // raw line, no trailing newline
  blank: boolean;
  foldable: boolean;
  rangeEnd: number; // exclusive index in lines[] where this line's child block ends
};

type Seg = { t: string; c?: string };

// ---- parsing ---------------------------------------------------------------

function leadingSpaces(s: string): number {
  let i = 0;
  while (i < s.length && s[i] === " ") i++;
  return i;
}

function parse(raw: string): { lines: Line[]; topKeys: { name: string; idx: number }[] } {
  const rawLines = raw.replace(/\n$/, "").split("\n");
  const n = rawLines.length;
  const blanks: boolean[] = rawLines.map((l) => l.trim() === "");
  const indents: number[] = rawLines.map((l, i) => (blanks[i] ? 0 : leadingSpaces(l)));

  const lines: Line[] = rawLines.map((text, i) => ({
    n: i + 1,
    indent: indents[i] ?? 0,
    text,
    blank: blanks[i] ?? false,
    foldable: false,
    rangeEnd: i + 1,
  }));

  for (let i = 0; i < n; i++) {
    const line = lines[i];
    if (!line || line.blank) continue;
    const d = line.indent;
    let j = i + 1;
    while (j < n && ((blanks[j] ?? false) || (indents[j] ?? 0) > d)) j++;
    // trim trailing blank lines out of the child block
    let end = j;
    while (end > i + 1 && (blanks[end - 1] ?? false)) end--;
    if (end > i + 1) {
      line.foldable = true;
      line.rangeEnd = end;
    }
  }

  const topKeys: { name: string; idx: number }[] = [];
  for (let i = 0; i < n; i++) {
    const line = lines[i];
    if (!line || line.blank || line.indent !== 0) continue;
    const m = line.text.match(/^([A-Za-z0-9_.-]+):/);
    if (m && m[1]) topKeys.push({ name: m[1], idx: i });
  }

  return { lines, topKeys };
}

// ---- tokenizing ------------------------------------------------------------

function findCommentIdx(s: string): number {
  let inS = false;
  let inD = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "'" && !inD) inS = !inS;
    else if (ch === '"' && !inS) inD = !inD;
    else if (ch === "#" && !inS && !inD && (i === 0 || s[i - 1] === " ")) return i;
  }
  return -1;
}

function classifyScalar(v: string): string {
  if (v.startsWith("=")) return "expr";
  if (/^['"]/.test(v)) return "str";
  if (/^-?\d+(\.\d+)?$/.test(v)) return "num";
  if (/^(true|false|null|~)$/i.test(v)) return "kw";
  return "plain";
}

function pushValue(segs: Seg[], v: string): void {
  if (v.startsWith("[") && v.endsWith("]")) {
    segs.push({ t: "[", c: "punct" });
    const inner = v.slice(1, -1);
    for (const part of inner.split(/(,)/)) {
      if (part === ",") {
        segs.push({ t: ",", c: "punct" });
        continue;
      }
      const trimmed = part.trim();
      if (trimmed === "") {
        segs.push({ t: part });
        continue;
      }
      // preserve surrounding whitespace, color the token
      const lead = part.slice(0, part.indexOf(trimmed));
      const tail = part.slice(lead.length + trimmed.length);
      if (lead) segs.push({ t: lead });
      segs.push({ t: trimmed, c: classifyScalar(trimmed) });
      if (tail) segs.push({ t: tail });
    }
    segs.push({ t: "]", c: "punct" });
    return;
  }
  segs.push({ t: v, c: classifyScalar(v) });
}

function tokenize(text: string): Seg[] {
  const segs: Seg[] = [];
  const indent = " ".repeat(leadingSpaces(text));
  if (indent) segs.push({ t: indent });
  let body = text.slice(indent.length);
  if (body === "") return segs;
  if (body.startsWith("#")) {
    segs.push({ t: body, c: "com" });
    return segs;
  }

  // list marker
  const lm = body.match(/^(-\s+)/);
  if (lm && lm[1]) {
    segs.push({ t: lm[1], c: "punct" });
    body = body.slice(lm[1].length);
  } else if (body === "-") {
    segs.push({ t: "-", c: "punct" });
    return segs;
  }

  // split off a trailing comment
  let comment = "";
  const ci = findCommentIdx(body);
  if (ci >= 0) {
    comment = body.slice(ci);
    body = body.slice(0, ci);
  }

  const kv = body.match(/^([^:\s][^:]*?):(\s*)(.*)$/);
  if (kv && kv[1] !== undefined && !body.startsWith("=")) {
    segs.push({ t: kv[1], c: "key" });
    segs.push({ t: ":", c: "punct" });
    if (kv[2]) segs.push({ t: kv[2] });
    if (kv[3]) pushValue(segs, kv[3]);
  } else if (body) {
    pushValue(segs, body);
  }

  if (comment) segs.push({ t: comment, c: "com" });
  return segs;
}

// Render one segment, wrapping any case-insensitive query matches in <mark>.
function renderSeg(seg: Seg, key: number, q: string): ReactNode {
  const cls = seg.c ? `tdn-yaml__${seg.c}` : undefined;
  if (!q) return <span key={key} className={cls}>{seg.t}</span>;
  const lower = seg.t.toLowerCase();
  const ql = q.toLowerCase();
  if (!lower.includes(ql)) return <span key={key} className={cls}>{seg.t}</span>;
  const parts: ReactNode[] = [];
  let from = 0;
  let at = lower.indexOf(ql, from);
  let pk = 0;
  while (at >= 0) {
    if (at > from) parts.push(seg.t.slice(from, at));
    parts.push(<mark key={pk++} className="tdn-yaml__hit">{seg.t.slice(at, at + ql.length)}</mark>);
    from = at + ql.length;
    at = lower.indexOf(ql, from);
  }
  if (from < seg.t.length) parts.push(seg.t.slice(from));
  return <span key={key} className={cls}>{parts}</span>;
}

// ---- component -------------------------------------------------------------

const EMPTY: ReadonlySet<number> = new Set();

export default function TdnYamlViewer({ raw, summary }: Props) {
  const { lines, topKeys } = useMemo(() => parse(raw), [raw]);
  const tokens = useMemo(() => lines.map((l) => (l.blank ? [] : tokenize(l.text))), [lines]);

  const [folded, setFolded] = useState<ReadonlySet<number>>(EMPTY);
  const [query, setQuery] = useState("");
  const [wrap, setWrap] = useState(false);
  const [active, setActive] = useState(0);
  const [jumpOpen, setJumpOpen] = useState(false);
  const [jumpActive, setJumpActive] = useState(0);
  const codeRef = useRef<HTMLDivElement>(null);
  const jumpRef = useRef<HTMLDivElement>(null);

  // Close the jump menu on outside click / Escape.
  useEffect(() => {
    if (!jumpOpen) return;
    const onDown = (e: MouseEvent) => {
      if (jumpRef.current && !jumpRef.current.contains(e.target as Node)) setJumpOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [jumpOpen]);

  const q = query.trim();

  const matches = useMemo(() => {
    if (!q) return [] as number[];
    const ql = q.toLowerCase();
    const out: number[] = [];
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      if (l && !l.blank && l.text.toLowerCase().includes(ql)) out.push(i);
    }
    return out;
  }, [q, lines]);

  // While searching, ignore folds so no match stays hidden.
  const effFolded = q ? EMPTY : folded;

  const visible = useMemo(() => {
    const out: number[] = [];
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (!l) break;
      out.push(i);
      if (l.foldable && effFolded.has(i)) i = l.rangeEnd;
      else i++;
    }
    return out;
  }, [lines, effFolded]);

  useEffect(() => {
    setActive(0);
  }, [q]);

  const scrollToLine = (idx: number) => {
    const el = codeRef.current?.querySelector(`[data-i="${idx}"]`);
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  };

  const gotoMatch = (dir: 1 | -1) => {
    if (matches.length === 0) return;
    const next = (active + dir + matches.length) % matches.length;
    setActive(next);
    const idx = matches[next];
    if (idx !== undefined) scrollToLine(idx);
  };

  const toggleFold = (i: number) => {
    setFolded((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  };

  const collapseAll = () => {
    // Fold every top-level foldable key -> a one-screen overview.
    const next = new Set<number>();
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      if (l && l.foldable && l.indent === 0) next.add(i);
    }
    setFolded(next);
  };
  const expandAll = () => setFolded(EMPTY);

  const jumpTo = (idx: number) => {
    if (q) setQuery("");
    setFolded((prev) => {
      if (!prev.has(idx)) return prev;
      const next = new Set(prev);
      next.delete(idx);
      return next;
    });
    requestAnimationFrame(() => scrollToLine(idx));
  };

  const summaryBits = [
    `${summary.operators} operator${summary.operators === 1 ? "" : "s"}`,
    `${summary.connections} connection${summary.connections === 1 ? "" : "s"}`,
    `${summary.annotations} annotation${summary.annotations === 1 ? "" : "s"}`,
    `${summary.lines} lines`,
  ];

  // Size the line-number gutter to the widest line number so the code text
  // stays aligned -- otherwise a 1-digit -> 2-digit -> 3-digit number widens the
  // column and the code drifts right (reads as spurious indentation).
  const lnDigits = Math.max(3, String(lines.length).length);

  return (
    <div className="tdn-yaml" style={{ "--tdn-ln-digits": lnDigits } as CSSProperties}>
      <div className="tdn-yaml__summary">{summaryBits.join("  ·  ")}</div>

      <div className="tdn-yaml__toolbar">
        <div className="tdn-yaml__search">
          <input
            type="search"
            className="tdn-yaml__search-input"
            placeholder="search..."
            aria-label="Search the TDN YAML"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {q && (
            <span className="tdn-yaml__search-nav">
              <span className="tdn-yaml__count">{matches.length ? `${active + 1}/${matches.length}` : "0"}</span>
              <button type="button" aria-label="Previous match" disabled={!matches.length} onClick={() => gotoMatch(-1)}>&#8593;</button>
              <button type="button" aria-label="Next match" disabled={!matches.length} onClick={() => gotoMatch(1)}>&#8595;</button>
            </span>
          )}
        </div>
        <div className="tdn-yaml__tools">
          {topKeys.length > 1 && (
            <div className="tdn-yaml__jump" ref={jumpRef}>
              <button
                type="button"
                className="tdn-yaml__jump-btn"
                aria-haspopup="listbox"
                aria-expanded={jumpOpen}
                onClick={() => { setJumpActive(0); setJumpOpen((o) => !o); }}
                onKeyDown={(e) => {
                  if (!jumpOpen && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    setJumpActive(0);
                    setJumpOpen(true);
                  }
                }}
              >
                <span>jump to...</span>
                <svg className="tdn-yaml__chev" width="9" height="6" viewBox="0 0 10 6" fill="none" aria-hidden="true"><path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.4" /></svg>
              </button>
              {jumpOpen && (
                <ul
                  className="tdn-yaml__jump-menu"
                  role="listbox"
                  aria-label="Jump to section"
                  aria-activedescendant={`tdn-jump-${jumpActive}`}
                  tabIndex={-1}
                  ref={(el) => el?.focus()}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      e.preventDefault();
                      setJumpOpen(false);
                      (jumpRef.current?.querySelector(".tdn-yaml__jump-btn") as HTMLElement | null)?.focus();
                    } else if (e.key === "ArrowDown") {
                      e.preventDefault();
                      setJumpActive((a) => Math.min(a + 1, topKeys.length - 1));
                    } else if (e.key === "ArrowUp") {
                      e.preventDefault();
                      setJumpActive((a) => Math.max(a - 1, 0));
                    } else if (e.key === "Home") {
                      e.preventDefault();
                      setJumpActive(0);
                    } else if (e.key === "End") {
                      e.preventDefault();
                      setJumpActive(topKeys.length - 1);
                    } else if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      const k = topKeys[jumpActive];
                      if (k) jumpTo(k.idx);
                      setJumpOpen(false);
                    }
                  }}
                >
                  {topKeys.map((k, i) => (
                    <li
                      key={k.idx}
                      id={`tdn-jump-${i}`}
                      role="option"
                      aria-selected={i === jumpActive}
                      className={`tdn-yaml__jump-opt${i === jumpActive ? " is-active" : ""}`}
                      onMouseEnter={() => setJumpActive(i)}
                      onClick={() => { jumpTo(k.idx); setJumpOpen(false); }}
                    >
                      {k.name}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <button type="button" className="tdn-yaml__btn" onClick={collapseAll}>collapse all</button>
          <button type="button" className="tdn-yaml__btn" onClick={expandAll}>expand all</button>
          <button
            type="button"
            className={`tdn-yaml__btn${wrap ? " is-on" : ""}`}
            aria-pressed={wrap}
            onClick={() => setWrap((w) => !w)}
          >
            wrap
          </button>
        </div>
      </div>

      <div ref={codeRef} className={`tdn-yaml__code${wrap ? " is-wrap" : ""}`}>
        {visible.map((i) => {
          const l = lines[i];
          if (!l) return null;
          const isFolded = l.foldable && folded.has(i) && !q;
          return (
            <div
              key={i}
              data-i={i}
              className={`tdn-yaml__line${matches[active] === i && q ? " is-active" : ""}`}
            >
              <span className="tdn-yaml__gutter">
                {l.foldable ? (
                  <button
                    type="button"
                    className="tdn-yaml__fold"
                    aria-label={isFolded ? "Expand section" : "Collapse section"}
                    aria-expanded={!isFolded}
                    onClick={() => toggleFold(i)}
                  >
                    {isFolded ? "▸" : "▾"}
                  </button>
                ) : (
                  <span className="tdn-yaml__fold tdn-yaml__fold--none" />
                )}
                <span className="tdn-yaml__ln">{l.n}</span>
              </span>
              <code className="tdn-yaml__text">
                {l.blank ? " " : (tokens[i] ?? []).map((s, k) => renderSeg(s, k, q))}
                {isFolded && <span className="tdn-yaml__ellipsis"> &#8943;</span>}
              </code>
            </div>
          );
        })}
      </div>
    </div>
  );
}
