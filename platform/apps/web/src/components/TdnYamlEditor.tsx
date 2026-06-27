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
  search, searchKeymap, highlightSelectionMatches, SearchQuery, setSearchQuery, findNext
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

export default function TdnYamlEditor({ name, initialValue = "", placeholder = "" }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const wrapComp = useRef(new Compartment());
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const [validity, setValidity] = useState(() => validateTdn(initialValue));
  const [wrap, setWrap] = useState(false);
  const [query, setQuery] = useState("");

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
    });

    // Paste of a full _embody_tdn JSON envelope -> replace the doc with its bare YAML.
    const pasteUnwrap = EditorView.domEventHandlers({
      paste(e, view) {
        const text = e.clipboardData?.getData("text");
        if (!text) return false;
        let parsed: unknown;
        try { parsed = parseYaml(text.trim()); } catch { return false; }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return false;
        const env = parsed as Record<string, unknown>;
        if (env[EMBODY_TDN_MARKER] !== EMBODY_TDN_VERSION || !env.tdn || typeof env.tdn !== "object") return false;
        const yamlText = stringifyYaml(env.tdn, { lineWidth: 0 }).replace(/\n$/, "");
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

  const runSearch = (q: string) => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: [
        setSearchTerm.of(q),
        setSearchQuery.of(new SearchQuery({ search: q, caseSensitive: false })),
      ],
    });
    if (q) findNext(view);
  };

  const doFoldAll = () => { const v = viewRef.current; if (v) { foldAll(v); v.focus(); } };
  const doUnfoldAll = () => { const v = viewRef.current; if (v) { unfoldAll(v); v.focus(); } };

  return (
    <div className="tdn-editor" data-valid={validity.valid ? "true" : "false"}>
      <div className="tdn-editor__toolbar">
        <input
          className="tdn-editor__search"
          type="search"
          placeholder="search..."
          aria-label="Search the TDN"
          value={query}
          onChange={(e) => { setQuery(e.target.value); runSearch(e.target.value); }}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); runSearch(query); } }}
        />
        <div className="tdn-editor__tools">
          <button type="button" className="tdn-editor__btn" title="Collapse all sections" onClick={doFoldAll}>&minus;</button>
          <button type="button" className="tdn-editor__btn" title="Expand all sections" onClick={doUnfoldAll}>+</button>
          <button
            type="button"
            className={`tdn-editor__btn${wrap ? " is-on" : ""}`}
            aria-pressed={wrap}
            title="Toggle word wrap"
            onClick={() => setWrap((w) => !w)}
          >
            wrap
          </button>
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
