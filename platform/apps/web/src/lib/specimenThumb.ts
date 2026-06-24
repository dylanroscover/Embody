type SpecimenThumbInput = {
  slug: string;
  category: string;
  name: string;
};

const PALETTE = {
  bg: "#181e1e",
  raised: "#1f2321",
  raised2: "#283028",
  code: "#161e1a",
  text: "#c8d0c9",
  muted: "#97a098",
  faint: "#6b756c",
  accent: "#6ee668",
  hover: "#a0f09c",
  leaf: "#9ccb5a",
  bark: "#c9954f",
  clay: "#d98a6a",
  mauve: "#b291b0",
  gold: "#d9c25a",
  sage: "#5fa777",
  stone: "#b9b09d"
};

export function specimenThumbnail(specimen: SpecimenThumbInput): string {
  const seed = hash(`${specimen.slug}:${specimen.category}`);
  const gradientA = shade(seed, 0);
  const gradientB = shade(seed, 1);
  const motif = motifFor(specimen.category, seed);
  const label = escapeXml(specimen.name);

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" role="img" aria-label="${label} placeholder">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="${gradientA}"/>
      <stop offset="1" stop-color="${gradientB}"/>
    </linearGradient>
    <radialGradient id="glow" cx="${26 + (seed % 48)}%" cy="${18 + (seed % 54)}%" r="70%">
      <stop offset="0" stop-color="${PALETTE.accent}" stop-opacity="0.18"/>
      <stop offset="0.46" stop-color="${PALETTE.accent}" stop-opacity="0.04"/>
      <stop offset="1" stop-color="${PALETTE.accent}" stop-opacity="0"/>
    </radialGradient>
    <filter id="soft">
      <feGaussianBlur stdDeviation="9"/>
    </filter>
  </defs>
  <rect width="640" height="360" fill="url(#bg)"/>
  <rect width="640" height="360" fill="url(#glow)"/>
  <path d="M0 72H640M0 144H640M0 216H640M0 288H640M80 0V360M160 0V360M240 0V360M320 0V360M400 0V360M480 0V360M560 0V360" stroke="${PALETTE.text}" stroke-opacity="0.05"/>
  ${motif}
  <text x="34" y="320" fill="${PALETTE.text}" fill-opacity="0.64" font-family="JetBrains Mono, ui-monospace, Menlo, Consolas, monospace" font-size="15" font-weight="600" letter-spacing="1.4">${escapeXml(specimen.category).toUpperCase()}</text>
</svg>`;

  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// Slugs with a baked RESULT render at /public/specimens/<slug>.jpg, captured from
// the live specimen networks. Anything not listed falls back to the procedural
// placeholder SVG above -- never a broken img.
//
// NOTE: the collection page's appended-card browser script keeps its own inline
// copy of this set (it runs in the client bundle); keep the two in sync.
export const BAKED_RESULTS = new Set([
  "kaleidoscope", "mandelbulb-march", "murmuration",
  "noise-terrain", "plasma-interference", "reaction-diffusion",
]);

/**
 * Resolve a specimen's RESULT cover image: the baked render for slugs in
 * BAKED_RESULTS, otherwise the procedural placeholder. `baked` lets callers
 * label the image (real result vs. "preview coming soon").
 */
export function resultImage(specimen: SpecimenThumbInput): { src: string; baked: boolean } {
  const baked = BAKED_RESULTS.has(specimen.slug);
  return {
    src: baked ? `/specimens/${specimen.slug}.jpg` : specimenThumbnail(specimen),
    baked,
  };
}

function motifFor(category: string, seed: number): string {
  switch (category) {
    case "generative-abstract":
      return noiseField(seed);
    case "feedback":
      return tunnelRings(seed);
    case "particles":
      return particleScatter(seed);
    case "audio-reactive":
      return spectrumBars(seed);
    case "raymarching-sdf":
      return sdfBlob(seed);
    case "compositing-post":
      return gradeBars(seed);
    default:
      return noiseField(seed);
  }
}

function noiseField(seed: number): string {
  const lines = Array.from({ length: 9 }, (_, index) => {
    const y = 70 + index * 22 + wave(seed, index, 12);
    const amp = 22 + (seed + index * 11) % 42;
    const color = pick(index, [PALETTE.accent, PALETTE.leaf, PALETTE.sage, PALETTE.gold]);
    return `<path d="M-20 ${y}C120 ${y - amp} 190 ${y + amp} 320 ${y}S500 ${y - amp} 660 ${y + wave(seed, index + 6, 18)}" fill="none" stroke="${color}" stroke-width="${2 + (index % 3)}" stroke-opacity="${0.24 + index * 0.035}"/>`;
  }).join("");

  return `<g>${lines}<circle cx="${430 + (seed % 70)}" cy="${96 + (seed % 84)}" r="84" fill="${PALETTE.accent}" fill-opacity="0.08" filter="url(#soft)"/></g>`;
}

function tunnelRings(seed: number): string {
  const cx = 300 + wave(seed, 2, 46);
  const cy = 175 + wave(seed, 4, 30);
  const rings = Array.from({ length: 8 }, (_, index) => {
    const r = 34 + index * 26;
    const color = pick(index, [PALETTE.accent, PALETTE.gold, PALETTE.bark, PALETTE.sage]);
    return `<ellipse cx="${cx}" cy="${cy}" rx="${r * 1.45}" ry="${r}" fill="none" stroke="${color}" stroke-width="${Math.max(2, 7 - index)}" stroke-opacity="${0.72 - index * 0.07}" transform="rotate(${(seed % 19) - 9} ${cx} ${cy})"/>`;
  }).join("");

  return `<g>${rings}<path d="M80 280C190 200 272 180 ${cx} ${cy}S500 90 610 36" fill="none" stroke="${PALETTE.accent}" stroke-width="3" stroke-opacity="0.34"/></g>`;
}

function particleScatter(seed: number): string {
  const dots = Array.from({ length: 52 }, (_, index) => {
    const x = 54 + pseudo(seed, index, 540);
    const y = 42 + pseudo(seed + 41, index, 276);
    const r = 2 + pseudo(seed + 91, index, 8) / 2;
    const color = pick(index, [PALETTE.accent, PALETTE.leaf, PALETTE.clay, PALETTE.gold, PALETTE.stone]);
    return `<circle cx="${x}" cy="${y}" r="${r}" fill="${color}" fill-opacity="${0.34 + (index % 5) * 0.08}"/>`;
  }).join("");
  const trails = Array.from({ length: 7 }, (_, index) => {
    const y = 76 + index * 38;
    return `<path d="M44 ${y}C160 ${y + wave(seed, index, 42)} 298 ${y - wave(seed, index + 3, 36)} 596 ${y + wave(seed, index + 5, 24)}" fill="none" stroke="${PALETTE.accent}" stroke-width="1.6" stroke-opacity="${0.12 + index * 0.02}"/>`;
  }).join("");

  return `<g>${trails}${dots}</g>`;
}

function spectrumBars(seed: number): string {
  const bars = Array.from({ length: 26 }, (_, index) => {
    const height = 36 + pseudo(seed, index, 160);
    const x = 58 + index * 20;
    const y = 272 - height;
    const color = pick(index, [PALETTE.accent, PALETTE.leaf, PALETTE.gold, PALETTE.bark, PALETTE.clay]);
    return `<rect x="${x}" y="${y}" width="10" height="${height}" rx="5" fill="${color}" fill-opacity="${0.44 + (index % 6) * 0.07}"/>`;
  }).join("");
  const mirror = Array.from({ length: 26 }, (_, index) => {
    const height = 18 + pseudo(seed + 17, index, 82);
    const x = 58 + index * 20;
    return `<rect x="${x}" y="276" width="10" height="${height}" rx="5" fill="${PALETTE.accent}" fill-opacity="0.14"/>`;
  }).join("");

  return `<g>${bars}${mirror}<path d="M42 276H590" stroke="${PALETTE.text}" stroke-opacity="0.12"/></g>`;
}

function sdfBlob(seed: number): string {
  const cx = 320 + wave(seed, 2, 34);
  const cy = 176 + wave(seed, 5, 24);
  const r1 = 90 + (seed % 28);
  const r2 = 74 + (seed % 22);
  return `<g>
    <ellipse cx="${cx}" cy="${cy}" rx="${r1}" ry="${r2}" fill="${PALETTE.sage}" fill-opacity="0.34" filter="url(#soft)"/>
    <path d="M${cx - 118} ${cy + 2}C${cx - 80} ${cy - 104} ${cx + 20} ${cy - 130} ${cx + 94} ${cy - 62}C${cx + 154} ${cy - 4} ${cx + 112} ${cy + 98} ${cx + 24} ${cy + 106}C${cx - 72} ${cy + 114} ${cx - 150} ${cy + 68} ${cx - 118} ${cy + 2}Z" fill="${PALETTE.bg}" stroke="${PALETTE.accent}" stroke-width="3" stroke-opacity="0.78"/>
    <path d="M${cx - 64} ${cy + 4}C${cx - 28} ${cy - 42} ${cx + 42} ${cy - 42} ${cx + 70} ${cy + 8}C${cx + 34} ${cy + 46} ${cx - 28} ${cy + 50} ${cx - 64} ${cy + 4}Z" fill="${PALETTE.gold}" fill-opacity="0.18" stroke="${PALETTE.hover}" stroke-width="2" stroke-opacity="0.48"/>
  </g>`;
}

function gradeBars(seed: number): string {
  const colors = [PALETTE.accent, PALETTE.leaf, PALETTE.gold, PALETTE.bark, PALETTE.clay, PALETTE.mauve, PALETTE.stone];
  const bars = colors.map((color, index) => {
    const width = 72 + pseudo(seed, index, 76);
    return `<rect x="${58 + index * 78}" y="${80 + index * 12}" width="${width}" height="34" rx="17" fill="${color}" fill-opacity="${0.34 + index * 0.06}"/>`;
  }).join("");
  const scopes = Array.from({ length: 4 }, (_, index) => {
    const y = 206 + index * 22;
    return `<path d="M72 ${y}C144 ${y - wave(seed, index, 28)} 220 ${y + wave(seed, index + 2, 20)} 306 ${y}S470 ${y - wave(seed, index + 4, 24)} 568 ${y}" fill="none" stroke="${pick(index, colors)}" stroke-width="2" stroke-opacity="0.38"/>`;
  }).join("");

  return `<g>${bars}${scopes}</g>`;
}

function shade(seed: number, offset: number): string {
  const greens = ["#151f1b", "#18261f", "#1b2b22", "#16211c", "#202a22", "#17251e"];
  return greens[(seed + offset * 7) % greens.length] ?? greens[0]!;
}

function pick(index: number, items: string[]): string {
  return items[index % items.length] ?? items[0]!;
}

function wave(seed: number, index: number, scale: number): number {
  return Math.round(Math.sin((seed % 29) + index * 1.9) * scale);
}

function pseudo(seed: number, index: number, range: number): number {
  const value = Math.sin(seed * 12.9898 + index * 78.233) * 43758.5453;
  return Math.floor((value - Math.floor(value)) * range);
}

function hash(value: string): number {
  let result = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function escapeXml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
