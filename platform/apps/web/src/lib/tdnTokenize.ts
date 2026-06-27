// Shared TDN/YAML line tokenizer: splits one line of TDN v2.0 YAML into colored
// segments (key / punctuation / string / number / keyword / expression / comment).
// Used by the read-only TdnYamlViewer and the editable TdnYamlEditor so both
// render identical syntax highlighting from a single source of truth. The TDN
// "=expression" shorthand gets its own class so it shows in the brand accent.

export type Seg = { t: string; c?: string };

export function leadingSpaces(s: string): number {
  let i = 0;
  while (i < s.length && s[i] === " ") i++;
  return i;
}

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

export function tokenize(text: string): Seg[] {
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
