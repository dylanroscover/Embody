import { useEffect, useRef, useState } from "react";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import {
  EditorView, keymap, lineNumbers, drawSelection, placeholder as cmPlaceholder,
  ViewPlugin, Decoration, type DecorationSet, type ViewUpdate
} from "@codemirror/view";
import { EditorState, Compartment, StateEffect, StateField, RangeSetBuilder } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { yaml } from "@codemirror/lang-yaml";
import {
  foldGutter, foldAll, unfoldAll, foldKeymap, codeFolding, foldService,
  syntaxHighlighting, HighlightStyle, indentOnInput, bracketMatching
} from "@codemirror/language";
import {
  search, searchKeymap, highlightSelectionMatches, SearchQuery, setSearchQuery, findNext, findPrevious
} from "@codemirror/search";
import { tags as t } from "@lezer/highlight";
import { EMBODY_TDN_MARKER, EMBODY_TDN_VERSION } from "@embody/contracts";

// Editable TDN/YAML editor for the contribute + edit forms, built on CodeMirror 6:
// native virtualization (smooth at 10k+ lines), indentation folding (the +/- toolbar
// buttons collapse/expand all sections), search, a wrap toggle, line numbers, and the
// same token palette as the read-only TdnYamlViewer. The CM doc is mirrored to a
// hidden <textarea name=...> so the form's FormData contract is unchanged, and a
// debounced `tdn:change` CustomEvent drives the form's submit gate. A full _embody_tdn
// JSON envelope pasted in is unwrapped to its bare YAML network.

type Props = {
  name: string;
  initialValue?: string;
  placeholder?: string;
};

export type TdnValidity = { valid: boolean; message: string };

export function validateTdn(text: string): TdnValidity {
  const s = text.trim();
  if (!s) return { valid: false, message: "" };
  let doc: unknown;
  try {
    doc = parseYaml(s);
  } catch {
    return { valid: false, message: "tdn must be valid YAML or JSON." };
  }
  if (doc === null || typeof doc !== "object" || Array.isArray(doc)) {
    return { valid: false, message: "tdn must parse to a mapping (object)." };
  }
  return { valid: true, message: "" };
}

// Indentation-based folding: a line whose following lines are MORE indented owns a
// foldable block (its deeper-indented body + trailing blank lines). Mirrors how the
// read-only viewer folds, so the +/- (fold all / unfold all) buttons fold real
// YAML sections.
const indentFold = foldService.of((state, lineStart) => {
  const line = state.doc.lineAt(lineStart);
  const indent = line.text.search(/\S/);
  if (indent < 0) return null;
  let end = line.to;
  let foundDeeper = false;
  for (let n = line.number + 1; n <= state.doc.lines; n++) {
    const next = state.doc.line(n);
    if (next.text.trim() === "") { end = next.to; continue; }   // blank lines join the block
    const nextIndent = next.text.search(/\S/);
    if (nextIndent <= indent) break;
    foundDeeper = true;
    end = next.to;
  }
  if (!foundDeeper) return null;
  return { from: line.to, to: end };
});

// Syntax colors mapped to the .tdn-yaml viewer palette (CSS vars resolve on .tdn-editor).
const tdnHighlight = HighlightStyle.define([
  { tag: [t.definition(t.propertyName), t.propertyName, t.atom], color: "var(--tdn-key)" },
  { tag: t.string, color: "var(--tdn-str)" },
  { tag: [t.number, t.integer, t.float], color: "var(--tdn-num)" },
  { tag: [t.bool, t.null, t.keyword], color: "var(--tdn-kw)" },
  { tag: t.comment, color: "var(--text-faint)", fontStyle: "italic" },
  { tag: [t.punctuation, t.separator, t.meta], color: "var(--text-muted)" },
]);

// Live highlight of every visible match of the toolbar search term. CM only paints
// .cm-searchMatch while its search panel is open; this works panel-less and only
// scans the visible ranges, so it stays cheap with virtualization.
const setSearchTerm = StateEffect.define<string>();
const searchTermField = StateField.define<string>({
  create: () => "",
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setSearchTerm)) return e.value;
    return value;
  },
});
const searchMark = Decoration.mark({ class: "cm-tdnMatch" });
function buildMatches(view: EditorView): DecorationSet {
  const term = view.state.field(searchTermField);
  const builder = new RangeSetBuilder<Decoration>();
  if (!term) return builder.finish();
  const needle = term.toLowerCase();
  for (const { from, to } of view.visibleRanges) {
    const hay = view.state.doc.sliceString(from, to).toLowerCase();
    let i = 0;
    while ((i = hay.indexOf(needle, i)) >= 0) {
      builder.add(from + i, from + i + term.length, searchMark);
      i += term.length;
    }
  }
  return builder.finish();
}
const searchHighlighter = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = buildMatches(view); }
    update(u: ViewUpdate) {
      if (u.docChanged || u.viewportChanged ||
          u.transactions.some((tr) => tr.effects.some((e) => e.is(setSearchTerm)))) {
        this.decorations = buildMatches(u.view);
      }
    }
  },
  { decorations: (v) => v.decorations }
);

const tdnTheme = EditorView.theme({
  "&": {
    height: "100%",
    color: "var(--text)",
    backgroundColor: "transparent",
    fontSize: "var(--tdn-fs)",
  },
  ".cm-scroller": {
    fontFamily: "var(--font-mono)",
    lineHeight: "var(--tdn-lh)",
    overflow: "auto",
  },
  ".cm-content": { caretColor: "var(--text)", padding: "var(--tdn-pad-y) 0" },
  ".cm-gutters": {
    backgroundColor: "var(--bg-code)",
    color: "var(--text-faint)",
    border: "none",
    borderRight: "1px solid var(--border-faint)",
    fontSize: "0.72rem",
  },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 0.6rem 0 0.85rem" },
  ".cm-foldGutter .cm-gutterElement": { cursor: "pointer", color: "var(--text-faint)" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--text)" },
  "&.cm-focused": { outline: "none" },
  ".cm-selectionBackground": { backgroundColor: "rgba(110, 230, 104, 0.16)" },
  "&.cm-focused .cm-selectionBackground, ::selection": { backgroundColor: "rgba(110, 230, 104, 0.22)" },
  ".cm-searchMatch": { backgroundColor: "rgba(110, 230, 104, 0.22)", borderRadius: "2px" },
  ".cm-searchMatch-selected": { backgroundColor: "rgba(110, 230, 104, 0.42)" },
  ".cm-tdnMatch": { backgroundColor: "rgba(110, 230, 104, 0.28)", borderRadius: "2px" },
  ".cm-activeLine": { backgroundColor: "rgba(255,255,255,0.02)" },
  ".cm-activeLineGutter": { backgroundColor: "transparent" },
  ".cm-foldPlaceholder": {
    backgroundColor: "transparent",
    border: "none",
    color: "var(--text-faint)",
    padding: "0 0.4rem",
  },
}, { dark: true });

// If `text` is a full _embody_tdn JSON/YAML envelope, return its bare TDN body
// as YAML; otherwise null. Shared by the editor's paste DOM handler and the
// toolbar "paste" button so both unwrap envelopes identically.
function unwrapTdnEnvelope(text: string): string | null {
  let parsed: unknown;
  try { parsed = parseYaml(text.trim()); } catch { return null; }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const env = parsed as Record<string, unknown>;
  if (env[EMBODY_TDN_MARKER] !== EMBODY_TDN_VERSION || !env.tdn || typeof env.tdn !== "object") return null;
  return stringifyYaml(env.tdn, { lineWidth: 0 }).replace(/\n$/, "");
}

export default function TdnYamlEditor({ name, initialValue = "", placeholder = "" }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const wrapComp = useRef(new Compartment());
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const [validity, setValidity] = useState(() => validateTdn(initialValue));
  const [wrap, setWrap] = useState(false);
  const [query, setQuery] = useState("");
  // Search match position: which occurrence is selected (1-based) of how many.
  const [matchTotal, setMatchTotal] = useState(0);
  const [matchPos, setMatchPos] = useState(0);
  const matchesRef = useRef<{ from: number; to: number }[]>([]);
  const queryRef = useRef("");
  // Go-to-line popover.
  const [gotoOpen, setGotoOpen] = useState(false);
  const [gotoValue, setGotoValue] = useState("");
  const gotoInputRef = useRef<HTMLInputElement>(null);

  const announce = (v: TdnValidity, doc: string) => {
    setValidity(v);
    taRef.current?.dispatchEvent(
      new CustomEvent("tdn:change", { bubbles: true, detail: { valid: v.valid, value: doc } })
    );
  };

  useEffect(() => {
    if (!hostRef.current) return;

    const onUpdate = EditorView.updateListener.of((u) => {
      if (!u.docChanged) return;
      const doc = u.state.doc.toString();
      if (taRef.current) taRef.current.value = doc;          // keep the form field current (immediate)
      clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => announce(validateTdn(doc), doc), 150);  // validate off the keystroke path
      // Keep the "current / total" search counter fresh when the doc changes
      // (e.g. a fresh paste) while a search is active.
      if (queryRef.current) {
        const hay = doc.toLowerCase();
        const needle = queryRef.current.toLowerCase();
        const ms: { from: number; to: number }[] = [];
        let j = 0;
        while ((j = hay.indexOf(needle, j)) >= 0) { ms.push({ from: j, to: j + needle.length }); j += needle.length || 1; }
        matchesRef.current = ms;
        setMatchTotal(ms.length);
        const sel = u.state.selection.main;
        const at = ms.findIndex((m) => m.from === sel.from && m.to === sel.to);
        setMatchPos(at >= 0 ? at + 1 : 0);
      }
    });

    // Paste of a full _embody_tdn JSON envelope -> replace the doc with its bare YAML.
    const pasteUnwrap = EditorView.domEventHandlers({
      paste(e, view) {
        const text = e.clipboardData?.getData("text");
        if (!text) return false;
        const yamlText = unwrapTdnEnvelope(text);
        if (yamlText === null) return false;
        e.preventDefault();
        view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: yamlText } });
        return true;
      },
    });

    const view = new EditorView({
      doc: initialValue,
      parent: hostRef.current,
      extensions: [
        lineNumbers(),
        codeFolding(),
        foldGutter(),
        indentFold,
        history(),
        drawSelection(),
        indentOnInput(),
        bracketMatching(),
        yaml(),
        syntaxHighlighting(tdnHighlight),
        search({ top: true }),
        highlightSelectionMatches(),
        searchTermField,
        searchHighlighter,
        keymap.of([...defaultKeymap, ...historyKeymap, ...foldKeymap, ...searchKeymap]),
        cmPlaceholder(placeholder),
        wrapComp.current.of([]),
        EditorState.tabSize.of(2),
        tdnTheme,
        onUpdate,
        pasteUnwrap,
      ],
    });
    viewRef.current = view;
    if (taRef.current) taRef.current.value = initialValue;
    announce(validateTdn(initialValue), initialValue);   // initial submit-gate state

    return () => { view.destroy(); viewRef.current = null; clearTimeout(debounceRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: wrapComp.current.reconfigure(wrap ? EditorView.lineWrapping : []),
    });
  }, [wrap]);

  // Focus the go-to-line input on open; close it on an outside click.
  useEffect(() => {
    if (!gotoOpen) return;
    gotoInputRef.current?.focus();
    const onDoc = (e: MouseEvent) => {
      const pop = gotoInputRef.current?.closest(".tdn-editor__goto");
      if (pop && !pop.contains(e.target as Node)) setGotoOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [gotoOpen]);

  // Scan the doc for every occurrence of the query (case-insensitive) so the
  // toolbar can show "current / total" and step through matches.
  const computeMatches = (q: string): { from: number; to: number }[] => {
    const view = viewRef.current;
    if (!view || !q) return [];
    const hay = view.state.doc.toString().toLowerCase();
    const needle = q.toLowerCase();
    const out: { from: number; to: number }[] = [];
    let i = 0;
    while ((i = hay.indexOf(needle, i)) >= 0) { out.push({ from: i, to: i + needle.length }); i += needle.length || 1; }
    return out;
  };

  // Reflect which match the editor's current selection sits on (1-based).
  const syncMatchPos = () => {
    const sel = viewRef.current?.state.selection.main;
    if (!sel) { setMatchPos(0); return; }
    const at = matchesRef.current.findIndex((m) => m.from === sel.from && m.to === sel.to);
    setMatchPos(at >= 0 ? at + 1 : 0);
  };

  const runSearch = (q: string) => {
    const view = viewRef.current;
    if (!view) return;
    queryRef.current = q;
    view.dispatch({
      effects: [
        setSearchTerm.of(q),
        setSearchQuery.of(new SearchQuery({ search: q, caseSensitive: false })),
      ],
    });
    matchesRef.current = computeMatches(q);
    setMatchTotal(matchesRef.current.length);
    if (q && matchesRef.current.length) { findNext(view); syncMatchPos(); }
    else setMatchPos(0);
  };

  const nextMatch = () => {
    const view = viewRef.current;
    if (!view || !matchesRef.current.length) return;
    findNext(view);
    syncMatchPos();
  };
  const prevMatch = () => {
    const view = viewRef.current;
    if (!view || !matchesRef.current.length) return;
    findPrevious(view);
    syncMatchPos();
  };
  const clearSearch = () => { setQuery(""); runSearch(""); };

  // Jump the caret to a 1-based line number (clamped to the doc) and center it.
  const doGoToLine = () => {
    const view = viewRef.current;
    const n = parseInt(gotoValue, 10);
    if (!view || !Number.isFinite(n)) { setGotoOpen(false); return; }
    const line = Math.max(1, Math.min(n, view.state.doc.lines));
    const pos = view.state.doc.line(line).from;
    view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "center" }) });
    view.focus();
    setGotoOpen(false);
    setGotoValue("");
  };

  const doFoldAll = () => { const v = viewRef.current; if (v) { foldAll(v); v.focus(); } };
  const doUnfoldAll = () => { const v = viewRef.current; if (v) { unfoldAll(v); v.focus(); } };

  // Replace the whole doc with the clipboard contents, unwrapping an _embody_tdn
  // envelope to its bare YAML body (same as a manual paste into the editor).
  const doPasteFromClipboard = async () => {
    const view = viewRef.current;
    if (!view) return;
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch { return; }
    if (!text) return;
    const insert = unwrapTdnEnvelope(text) ?? text;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert } });
    view.focus();
  };

  return (
    <div className="tdn-editor" data-valid={validity.valid ? "true" : "false"}>
      <div className="tdn-editor__toolbar">
        <div className="tdn-editor__searchbox">
          <svg className="tdn-editor__search-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <circle cx="11" cy="11" r="7" />
            <path d="M21 21l-4.3-4.3" />
          </svg>
          <input
            className="tdn-editor__search"
            type="text"
            placeholder=""
            aria-label="Search the TDN"
            value={query}
            onChange={(e) => { setQuery(e.target.value); runSearch(e.target.value); }}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); if (e.shiftKey) prevMatch(); else nextMatch(); }
              else if (e.key === "Escape") { e.preventDefault(); clearSearch(); }
            }}
          />
          {query && (
            <div className="tdn-editor__search-nav">
              <span className="tdn-editor__search-count" aria-live="polite">{matchTotal ? `${matchPos}/${matchTotal}` : "0/0"}</span>
              <button type="button" className="tdn-editor__search-navbtn" title="Previous match (Shift+Enter)" aria-label="Previous match" onClick={prevMatch} disabled={!matchTotal}>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="6 14 12 8 18 14" /></svg>
              </button>
              <button type="button" className="tdn-editor__search-navbtn" title="Next match (Enter)" aria-label="Next match" onClick={nextMatch} disabled={!matchTotal}>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="6 10 12 16 18 10" /></svg>
              </button>
              <button type="button" className="tdn-editor__search-navbtn tdn-editor__search-navbtn--clear" title="Clear search" aria-label="Clear search" onClick={clearSearch}>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
              </button>
            </div>
          )}
        </div>
        <div className="tdn-editor__tools">
          <button type="button" className="tdn-editor__btn" title="Paste TDN from clipboard" aria-label="Paste TDN from clipboard" onClick={doPasteFromClipboard}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
              <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
            </svg>
          </button>
          <button type="button" className="tdn-editor__btn" title="Collapse all sections" onClick={doFoldAll}>&minus;</button>
          <button type="button" className="tdn-editor__btn" title="Expand all sections" onClick={doUnfoldAll}>+</button>
          <button
            type="button"
            className={`tdn-editor__btn${wrap ? " is-on" : ""}`}
            aria-pressed={wrap}
            title="Toggle word wrap"
            aria-label="Toggle word wrap"
            onClick={() => setWrap((w) => !w)}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <line x1="3" y1="6" x2="21" y2="6" />
              <path d="M3 12h15a3 3 0 1 1 0 6h-4" />
              <polyline points="16 16 14 18 16 20" />
              <line x1="3" y1="18" x2="10" y2="18" />
            </svg>
          </button>
          <div className="tdn-editor__goto">
            <button
              type="button"
              className={`tdn-editor__btn${gotoOpen ? " is-on" : ""}`}
              title="Go to line"
              aria-label="Go to line"
              aria-expanded={gotoOpen}
              onClick={() => setGotoOpen((o) => !o)}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="10" y1="6" x2="20" y2="6" />
                <line x1="10" y1="12" x2="20" y2="12" />
                <line x1="10" y1="18" x2="20" y2="18" />
                <polyline points="3 8 5.5 12 3 16" />
              </svg>
            </button>
            {gotoOpen && (
              <div className="tdn-editor__goto-pop">
                <input
                  ref={gotoInputRef}
                  className="tdn-editor__goto-input"
                  type="number"
                  min="1"
                  inputMode="numeric"
                  placeholder="line #"
                  aria-label="Go to line number"
                  value={gotoValue}
                  onChange={(e) => setGotoValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { e.preventDefault(); doGoToLine(); }
                    else if (e.key === "Escape") { e.preventDefault(); setGotoOpen(false); }
                  }}
                />
                <button type="button" className="tdn-editor__goto-go" onClick={doGoToLine}>go</button>
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="tdn-editor__cm" ref={hostRef} />
      <textarea
        ref={taRef}
        name={name}
        defaultValue={initialValue}
        hidden
        tabIndex={-1}
        aria-hidden="true"
      />
      <small
        id="tdn-editor-error"
        className="tdn-editor__error"
        role="alert"
        hidden={!validity.message}
      >
        {validity.message}
      </small>
    </div>
  );
}
