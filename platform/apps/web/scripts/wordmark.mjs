// Generates src/components/Wordmark.astro: the "embody" wordmark with the
// enclosing loop, as static SVG paths derived from the shipped Inter SemiBold
// outlines (public/fonts/inter-latin-600-normal.woff2). Static paths, not live
// text, so the loop's join with the y's tail and the b's stem is exact in every
// browser and needs no font to be loaded.
//
// The loop is the y's own tail continued: Inter's y hooks left and ends in a
// short slanted cut about 15 units below the baseline; the stroke leaves that
// cut heading left at the hook's depth and width, runs under the word, follows
// the e's own outer curve round its left at a steady distance (an offset of
// the glyph outline, so the loop's rounding IS the e's), comes back along the
// ascender line, and turns down into the b's stem, matching the stem's width
// so the two merge.
//
// Run:  node scripts/wordmark.mjs        (needs fontkit resolvable: either
//       `npm i --no-save fontkit` here, or FONTKIT_DIR=<dir with node_modules>)
import { createRequire } from "node:module";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(process.env.FONTKIT_DIR ? join(process.env.FONTKIT_DIR, "package.json") : import.meta.url);
const fontkit = require("fontkit");

const FONT = join(here, "..", "public", "fonts", "inter-latin-600-normal.woff2");
const OUT = join(here, "..", "src", "components", "Wordmark.astro");
const WORD = "embody";
const TRACK = 2.5; // extra units between letters (~0.025em), a touch of tracking at small sizes
const BASE = 100; // SVG y of the baseline; 1em = 100 units

const font = fontkit.openSync(FONT);
const S = 100 / font.unitsPerEm;
const run = font.layout(WORD);
const f1 = (n) => (Math.round(n * 10) / 10).toString();

// --- letters as one path (font y up -> SVG y down) ---
let x = 0;
const glyphs = [];
for (const [i, g] of run.glyphs.entries()) {
  glyphs.push({ ch: WORD[i], g, x });
  x += run.positions[i].xAdvance * S + TRACK;
}
const letters = glyphs.map(({ g, x }) => g.path.transform(S, 0, 0, -S, x, BASE).toSVG()).join(" ");

// --- geometry (font units scaled, y up, local to each glyph) ---
const pts = (g) => g.path.commands.filter((c) => c.command !== "closePath").map((c) => ({ x: c.args[c.args.length - 2] * S, y: c.args[c.args.length - 1] * S }));
const gy = glyphs[5], gb = glyphs[2], ge = glyphs[0];

// The y's terminal cut: its two lowest-left vertices (outer edge end, inner edge start).
const yp = pts(gy.g).filter((p) => p.y < -8);
const cutA = yp.reduce((a, p) => (p.x < a.x ? p : a)); // (6.2, -19.5) outer end
const cutB = yp.filter((p) => p.x < 12 && p.y > -12).reduce((a, p) => (p.x < a.x ? p : a)); // (9.2, -9.6) inner end
// The stroke starts 3 units inside the hook. Its width and depth are the
// hook's own at that x: sample the inner and outer edges there (linear
// between the outline's vertices; the curves are shallow here).
const startLocalX = (cutA.x + cutB.x) / 2 + 3;
const edgeYAt = (edge, x) => {
  const s = edge.slice().sort((a, b) => a.x - b.x);
  for (let i = 0; i + 1 < s.length; i++) if (s[i].x <= x && x <= s[i + 1].x) return s[i].y + (s[i + 1].y - s[i].y) * ((x - s[i].x) / (s[i + 1].x - s[i].x));
  return s[0].y;
};
const inner = yp.filter((p) => p.y > -15 && p.x < 20), outer = yp.filter((p) => p.y <= -15 && p.x < 20);
const innerY = edgeYAt(inner, startLocalX), outerY = edgeYAt(outer, startLocalX);
const tailW = innerY - outerY;
const runY = (innerY + outerY) / 2; // the under-run keeps the hook's depth
const tailMid = { x: startLocalX - 3 + gy.x, y: runY };

// The b's stem: vertices on its top edge.
const bp = pts(gb.g);
const bTop = Math.max(...bp.map((p) => p.y));
const stemXs = bp.filter((p) => p.y > bTop - 0.5).map((p) => p.x);
const stemL = Math.min(...stemXs) + gb.x, stemR = Math.max(...stemXs) + gb.x;
const stemW = stemR - stemL;
const stemC = (stemL + stemR) / 2;
const W = tailW; // the stroke is the tail's width; it ends flush inside the wider stem top
const topY = bTop - W / 2; // over-run centreline: its top edge is the stem's top

// The e's outer left contour: the outline's first six quadratics run from the
// bottom-most point round the left to the top-most point, with horizontal
// tangents at both ends. Offsetting that arc outward gives a loop side that is
// concentric with the e everywhere. The offset distance is set by the two
// straight runs it has to meet: the under-run sits eBottom - runY below the e
// and the over-run topY - eTop above it, so the gap eases between those two
// values with height (a couple of units over the whole side, invisible) and
// the arc lands on each run exactly tangent, no corner.
const ecmds = ge.g.path.commands;
const eArc = []; // flattened outer-left contour, glyph-local font units (y up)
{
  let cur = null;
  for (const c of ecmds) {
    if (c.command === "moveTo") { cur = { x: c.args[0] * S, y: c.args[1] * S }; eArc.push(cur); continue; }
    if (c.command !== "quadraticCurveTo") break; // the arc is the leading run of quadratics
    const [cx, cy, x1, y1] = c.args.map((a) => a * S);
    const N = 28;
    for (let i = 1; i <= N; i++) {
      const t = i / N, u = 1 - t;
      eArc.push({ x: u * u * cur.x + 2 * u * t * cx + t * t * x1, y: u * u * cur.y + 2 * u * t * cy + t * t * y1 });
    }
    cur = { x: x1, y: y1 };
    if (eArc.length > 6 * N) break; // six quadratics: bottom point -> left -> top point
  }
}
const eTop = ge.g.bbox.maxY * S, eBottom = ge.g.bbox.minY * S;
const gapBottom = eBottom - runY; // centreline distance below the e at the under-run
const gapTop = topY - eTop; // ...and above it at the over-run
const eCentre = { x: (ge.g.bbox.minX + ge.g.bbox.maxX) * S / 2, y: (eTop + eBottom) / 2 };
const offset = eArc.map((p, i) => {
  const a = eArc[Math.max(0, i - 1)], b = eArc[Math.min(eArc.length - 1, i + 1)];
  let nx = -(b.y - a.y), ny = b.x - a.x; // a normal to the local tangent
  const len = Math.hypot(nx, ny) || 1;
  nx /= len; ny /= len;
  if (nx * (p.x - eCentre.x) + ny * (p.y - eCentre.y) < 0) { nx = -nx; ny = -ny; } // outward
  const g = gapTop + (gapBottom - gapTop) * ((eTop - p.y) / (eTop - eBottom));
  return { x: p.x + nx * g + ge.x, y: p.y + ny * g };
});
// Thin the polyline to ~1.2-unit steps: at this radius that is under 2 degrees
// per segment, and round joins hide the rest.
const side = [offset[0]];
for (const p of offset) if (Math.hypot(p.x - side[side.length - 1].x, p.y - side[side.length - 1].y) >= 1.2) side.push(p);
if (side[side.length - 1] !== offset[offset.length - 1]) side.push(offset[offset.length - 1]);
const L = Math.min(...side.map((p) => p.x)); // the loop's leftmost centreline x, for the viewBox
// Gap check for the log: clear space between the e's ink and the stroke's
// inner edge along the side (should sit between gapTop - W/2 and gapBottom - W/2).
const clear = side.map((q) => Math.min(...eArc.map((p) => Math.hypot(p.x + ge.x - q.x, p.y - q.y))) - W / 2);

const Y = (fy) => BASE - fy; // font y -> SVG y
const d = [
  // start 3 units inside the hook (its slanted terminal cut would otherwise
  // leave a notch beside a square cap) and continue straight out of it at the
  // hook's own depth: no easing, no wobble
  `M${f1(tailMid.x + 3)} ${f1(Y(runY))}`,
  // under the word to the e's bottom point, then round its left on the offset
  // arc (its first point is on the under-run, its last on the over-run)
  ...side.map((p) => `L${f1(p.x)} ${f1(Y(p.y))}`),
  // along the ascender line into the top of the b's stem: a square corner,
  // like the stem's own top, so the stem reads as turning left into the loop
  `L${f1(stemR - 1)} ${f1(Y(topY))}`
].join(" ");

// viewBox centred on the x-height band (so a flex parent centres the letters'
// visual mass), wide enough for the enclosure and the y.
const xhMid = Y(font.xHeight * S / 2);
const top = Y(bTop) - 6;
const half = xhMid - top;
const right = gy.x + gy.g.bbox.maxX * S + 3;
const left = L - W / 2 - 3;
const vb = `${f1(left)} ${f1(top)} ${f1(right - left)} ${f1(half * 2)}`;

const astro = `---
// GENERATED by scripts/wordmark.mjs from public/fonts/inter-latin-600-normal.woff2
// -- do not hand-edit; re-run the script. The "embody" wordmark with the loop
// that encloses it: the y's tail continued under the word, round the e, and
// back along the ascender line into the b's stem. Static paths (not live
// text) so the joins are exact in every browser. Colour is currentColor.
interface Props {
  class?: string;
}
const { class: className = "" } = Astro.props;
---

<svg class={\`wordmark \${className}\`.trim()} viewBox="${vb}" role="img" aria-label="embody">
  <path class="wordmark__letters" fill="currentColor" d="${letters}"></path>
  <path class="wordmark__loop" fill="none" stroke="currentColor" stroke-width="${f1(W)}" stroke-linejoin="round" stroke-linecap="butt" d="${d}"></path>
</svg>
`;
writeFileSync(OUT, astro, "utf8");
console.log(JSON.stringify({ tailMid, tailW: f1(tailW), stemL: f1(stemL), stemR: f1(stemR), stemW: f1(stemW), W: f1(W), topY: f1(topY), gapTop: f1(gapTop), gapBottom: f1(gapBottom), sidePoints: side.length, L: f1(L), clearMin: f1(Math.min(...clear)), clearMax: f1(Math.max(...clear)), viewBox: vb, height_em: f1(half * 2 / 100) }));
