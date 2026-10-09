// Syntax highlighting for the code a TDXN DAT carries in `dat_content: |` --
// Python, GLSL, JSON, XML (YAML reuses the TDXN tokenizer). Small, line-at-a-time
// scanners that emit the SAME segment classes as tdxnTokenize, so code reads in the
// viewer's own palette: com (comments), str, num, kw (keywords), key (types,
// builtins, called functions, JSON keys), expr (preprocessor, decorators,
// TouchDesigner builtins), punct. A block is scanned top to bottom so multi-line
// constructs -- GLSL /* */ comments, Python triple-quoted strings, XML comments --
// carry their state from line to line.

import { tokenize as tokenizeYaml, type Seg } from "./tdxnTokenize";

export type CodeLang = "python" | "glsl" | "json" | "yaml" | "xml" | "text";

const PY_KW = new Set(("False None True and as assert async await break class continue def del elif else except " +
  "finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case").split(" "));
const PY_BUILTIN = new Set(("print len range int float str bool list dict set tuple min max abs round sum any all " +
  "enumerate zip map filter sorted isinstance getattr setattr hasattr super open type repr " +
  "op ops parent me absTime project ui run debug tdu mod iop ipar").split(" "));
const GLSL_KW = new Set(("if else for while do return break continue discard switch case default struct const " +
  "uniform in out inout layout flat smooth noperspective highp mediump lowp precision true false").split(" "));
const GLSL_TYPE = /^(void|bool|int|uint|float|double|[biud]?vec[234]|d?mat[234](x[234])?|[iu]?sampler\w*|[iu]?image\w*)$/;

type State = { block?: "py3" | "glslc" | "xmlc"; quote?: string };

const push = (segs: Seg[], t: string, c?: string) => { if (t) segs.push(c ? { t, c } : { t }); };

function scanPython(s: string, st: State): Seg[] {
  const segs: Seg[] = [];
  let i = 0;
  if (st.block === "py3" && st.quote) {
    const end = s.indexOf(st.quote);
    if (end < 0) return [{ t: s, c: "str" }];
    push(segs, s.slice(0, end + 3), "str");
    i = end + 3;
    st.block = undefined;
  }
  while (i < s.length) {
    const rest = s.slice(i);
    let m: RegExpMatchArray | null;
    if (rest[0] === "#") { push(segs, rest, "com"); break; }
    if ((m = rest.match(/^[rRbBuUfF]{0,2}("""|''')/))) {
      const q = m[1] as string;
      const close = rest.indexOf(q, m[0].length);
      if (close < 0) { push(segs, rest, "str"); st.block = "py3"; st.quote = q; break; }
      push(segs, rest.slice(0, close + 3), "str");
      i += close + 3;
      continue;
    }
    if ((m = rest.match(/^[rRbBuUfF]{0,2}("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?)/))) { push(segs, m[0], "str"); i += m[0].length; continue; }
    if ((m = rest.match(/^@[\w.]+/))) { push(segs, m[0], "expr"); i += m[0].length; continue; }
    if ((m = rest.match(/^(0[xX][\da-fA-F_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?j?|\.\d+)/))) { push(segs, m[0], "num"); i += m[0].length; continue; }
    if ((m = rest.match(/^[A-Za-z_]\w*/))) {
      const w = m[0];
      const called = /^\s*\(/.test(rest.slice(w.length));
      push(segs, w, PY_KW.has(w) ? "kw" : PY_BUILTIN.has(w) || called ? "key" : undefined);
      i += w.length;
      continue;
    }
    if ((m = rest.match(/^\s+/))) { push(segs, m[0]); i += m[0].length; continue; }
    push(segs, rest[0] as string, "punct");
    i += 1;
  }
  return segs;
}

function scanGlsl(s: string, st: State): Seg[] {
  const segs: Seg[] = [];
  let i = 0;
  if (st.block === "glslc") {
    const end = s.indexOf("*/");
    if (end < 0) return [{ t: s, c: "com" }];
    push(segs, s.slice(0, end + 2), "com");
    i = end + 2;
    st.block = undefined;
  } else if (/^\s*#/.test(s)) {
    return [{ t: s, c: "expr" }];
  }
  while (i < s.length) {
    const rest = s.slice(i);
    let m: RegExpMatchArray | null;
    if (rest.startsWith("//")) { push(segs, rest, "com"); break; }
    if (rest.startsWith("/*")) {
      const end = rest.indexOf("*/", 2);
      if (end < 0) { push(segs, rest, "com"); st.block = "glslc"; break; }
      push(segs, rest.slice(0, end + 2), "com");
      i += end + 2;
      continue;
    }
    if ((m = rest.match(/^(0[xX][\da-fA-F]+[uU]?|(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?[uUfF]?)/))) { push(segs, m[0], "num"); i += m[0].length; continue; }
    if ((m = rest.match(/^[A-Za-z_]\w*/))) {
      const w = m[0];
      const cls = GLSL_KW.has(w) ? "kw"
        : GLSL_TYPE.test(w) ? "key"
        : /^(TD|sTD|uTD|vUV|gl_)/.test(w) ? "expr"
        : /^\s*\(/.test(rest.slice(w.length)) ? "key" : undefined;
      push(segs, w, cls);
      i += w.length;
      continue;
    }
    if ((m = rest.match(/^\s+/))) { push(segs, m[0]); i += m[0].length; continue; }
    push(segs, rest[0] as string, "punct");
    i += 1;
  }
  return segs;
}

function scanJson(s: string): Seg[] {
  const segs: Seg[] = [];
  let i = 0;
  while (i < s.length) {
    const rest = s.slice(i);
    let m: RegExpMatchArray | null;
    if ((m = rest.match(/^"(?:[^"\\]|\\.)*"?/))) {
      push(segs, m[0], /^\s*:/.test(rest.slice(m[0].length)) ? "key" : "str");
      i += m[0].length;
      continue;
    }
    if ((m = rest.match(/^-?\d+(\.\d+)?([eE][+-]?\d+)?/))) { push(segs, m[0], "num"); i += m[0].length; continue; }
    if ((m = rest.match(/^(true|false|null)\b/))) { push(segs, m[0], "kw"); i += m[0].length; continue; }
    if ((m = rest.match(/^\s+/))) { push(segs, m[0]); i += m[0].length; continue; }
    push(segs, rest[0] as string, "punct");
    i += 1;
  }
  return segs;
}

function scanXml(s: string, st: State): Seg[] {
  const segs: Seg[] = [];
  let i = 0;
  if (st.block === "xmlc") {
    const end = s.indexOf("-->");
    if (end < 0) return [{ t: s, c: "com" }];
    push(segs, s.slice(0, end + 3), "com");
    i = end + 3;
    st.block = undefined;
  }
  while (i < s.length) {
    const rest = s.slice(i);
    let m: RegExpMatchArray | null;
    if (rest.startsWith("<!--")) {
      const end = rest.indexOf("-->", 4);
      if (end < 0) { push(segs, rest, "com"); st.block = "xmlc"; break; }
      push(segs, rest.slice(0, end + 3), "com");
      i += end + 3;
      continue;
    }
    if ((m = rest.match(/^<\/?[\w:.-]+/))) { push(segs, m[0], "key"); i += m[0].length; continue; }
    if ((m = rest.match(/^"[^"]*"?|^'[^']*'?/))) { push(segs, m[0], "str"); i += m[0].length; continue; }
    if ((m = rest.match(/^[\w:.-]+(?==)/))) { push(segs, m[0], "kw"); i += m[0].length; continue; }
    if ((m = rest.match(/^\/?>/))) { push(segs, m[0], "key"); i += m[0].length; continue; }
    if ((m = rest.match(/^[^<"'=>/]+/))) { push(segs, m[0]); i += m[0].length; continue; }
    push(segs, rest[0] as string, "punct");
    i += 1;
  }
  return segs;
}

// Tokenize a dat_content block. Each line keeps its YAML block indentation
// (`indent` spaces) as plain text; the code after it is scanned in `lang`.
export function tokenizeCodeBlock(lines: string[], lang: CodeLang, indent: number): Seg[][] {
  const st: State = {};
  return lines.map((line) => {
    if (line.trim() === "") return [];
    const pad = line.slice(0, Math.min(indent, line.length - line.trimStart().length));
    const code = line.slice(pad.length);
    const segs: Seg[] = pad ? [{ t: pad }] : [];
    const body = lang === "python" ? scanPython(code, st)
      : lang === "glsl" ? scanGlsl(code, st)
      : lang === "json" ? scanJson(code)
      : lang === "xml" ? scanXml(code, st)
      : lang === "yaml" ? tokenizeYaml(code)
      : [{ t: code, c: "plain" }];
    return segs.concat(body);
  });
}

const EXT_LANG: Record<string, CodeLang> = {
  py: "python", frag: "glsl", vert: "glsl", glsl: "glsl", comp: "glsl", geom: "glsl", tesc: "glsl", tese: "glsl",
  json: "json", yaml: "yaml", yml: "yaml", xml: "xml", html: "xml", svg: "xml", txt: "text", csv: "text", tsv: "text",
};

// Pick a DAT's language: its `language` parameter, then its file `extension`,
// then its type (execute/script DATs are Python), then GLSL dock names, then a
// look at the code itself.
export function detectLanguage(fields: { type?: string; language?: string; extension?: string; name?: string }, code: string): CodeLang {
  const lang = fields.language?.toLowerCase();
  if (lang && ["python", "glsl", "json", "yaml", "xml", "text"].includes(lang)) return lang as CodeLang;
  const ext = fields.extension?.toLowerCase().replace(/^\./, "");
  if (ext && EXT_LANG[ext]) return EXT_LANG[ext] as CodeLang;
  if (fields.type && /(execute|script)(dat|chop|sop|top)$/i.test(fields.type)) return "python";
  if (fields.name && /_(pixel|vertex|compute|glsl)$/.test(fields.name)) return "glsl";
  const head = code.trimStart();
  if (/^[[{]/.test(head)) { try { JSON.parse(code); return "json"; } catch { /* not json */ } }
  if (/^\s*(#version|uniform |layout\s*\(|void main)|\bvec[234]\b/m.test(code)) return "glsl";
  if (/^\s*(def |class |import |from \w+ import )|^\s*(""")/m.test(code)) return "python";
  if (/^\s*</.test(head)) return "xml";
  return "text";
}
