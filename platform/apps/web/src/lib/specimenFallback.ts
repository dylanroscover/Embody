import type { SpecimenSummary } from "@embody/contracts";

// The six real first-party Specimens (REPO/specimens/manifest.json) embedded as
// the fallback set. This guarantees the collection page and the /c/[slug] detail
// page show the real specimens even when local D1 is empty or unreachable - the
// page never falls back to fictional placeholders.
//
// Shape mirrors the fields the UI reads: collection/index.astro consumes
// fixtureSummaries (SpecimenSummary), and c/[slug].astro reads slug, name,
// category, level, description, requires, operator_count, key_ops, prompt.
// tags / want / evolve back the client-side search helper.

export interface FixtureSpecimen {
  slug: string;
  name: string;
  category: string;
  level: string;
  description: string;
  tags: string[];
  requires: string;
  operator_count: number;
  key_ops: string[];
  want: string;
  prompt: string;
  evolve: string;
}

const fixtureSpecimens: FixtureSpecimen[] = [
  {
    slug: "reaction-diffusion",
    name: "Reaction-Diffusion (Gray-Scott)",
    category: "generative",
    level: "intermediate",
    description:
      "A living Gray-Scott reaction-diffusion field. Two chemicals diffuse and react in a GPU feedback loop, growing organic maze and coral patterns that evolve continuously. Usable as a texture, displacement, or mask source.",
    tags: ["feedback", "glsl", "generative", "simulation", "texture"],
    requires: "none",
    operator_count: 14,
    key_ops: ["feedbackTOP", "glslTOP"],
    want: "A living generative texture",
    prompt: "embody the reaction-diffusion specimen into my project",
    evolve:
      "Slow the feed/kill drift and tint the coral growth with a warm palette."
  },
  {
    slug: "kaleidoscope",
    name: "Kaleidoscope",
    category: "compositing",
    level: "intermediate",
    description:
      "A reusable kaleidoscope compositor. Folds any TOP (or its built-in animated source) into an N-fold mirrored mandala that rotates, twists, breathes, and tumbles. Drop it onto any visual via the External source mode.",
    tags: ["compositing", "glsl", "mirror", "symmetry", "effect"],
    requires: "none",
    operator_count: 11,
    key_ops: ["glslTOP", "switchTOP", "inTOP"],
    want: "A mirrored mandala compositor",
    prompt: "embody the kaleidoscope specimen into my project",
    evolve:
      "Wire your own visual into the External input and raise the segment count."
  },
  {
    slug: "noise-terrain",
    name: "Ridged Mountain Terrain",
    category: "3d",
    level: "advanced",
    description:
      "A procedural snow-mountain scene. A GLSL POP compute shader displaces a grid into ridged-multifractal peaks that morph in place, shaded by a snow/rock GLSL MAT with elevation-based snow, sun/sky lighting, and atmospheric haze, composited under a procedural sky.",
    tags: ["3d", "glsl", "terrain", "geometry", "procedural"],
    requires: "none",
    operator_count: 20,
    key_ops: ["glslPOP", "glslMAT", "gridPOP", "renderTOP"],
    want: "A procedural alpine landscape",
    prompt: "embody the noise-terrain specimen into my project",
    evolve:
      "Lower the snow line and drift the sun across the peaks over a slow cycle."
  },
  {
    slug: "murmuration",
    name: "Murmuration",
    category: "simulation",
    level: "advanced",
    description:
      "A dense GPU particle swarm that flocks like a starling murmuration at dusk - cohering, separating, aligning, and flowing around a slow invisible attractor with curl-noise wander. True per-neighbor Reynolds flocking computed on the GPU (a Neighbor POP index list iterated in a GLSL POP), rendered as luminous additive point sprites.",
    tags: ["simulation", "glsl", "pop", "flocking", "particles", "generative"],
    requires: "none",
    operator_count: 18,
    key_ops: ["particlePOP", "neighborPOP", "glslPOP", "pointspriteMAT"],
    want: "A flocking particle swarm",
    prompt: "embody the murmuration specimen into my project",
    evolve:
      "Tighten the separation radius and let the attractor wander more widely."
  },
  {
    slug: "plasma-interference",
    name: "Plasma (Sine Interference)",
    category: "generative",
    level: "intermediate",
    description:
      "A flowing GPU plasma. Two sine-wave fields at slightly detuned scales beat against each other into shimmering moire fringes, a slow rotating domain warp bends the coordinates into liquid motion, and the result is mapped through a cyclic cosine palette. Self-contained and stateless - one GLSL TOP, no input, no feedback - so it drops in anywhere as a VJ loop, texture, or displacement source.",
    tags: ["generative", "glsl", "plasma", "palette", "vj", "texture"],
    requires: "none",
    operator_count: 5,
    key_ops: ["glslTOP"],
    want: "A flowing VJ plasma loop",
    prompt: "embody the plasma-interference specimen into my project",
    evolve:
      "Detune the two fields further and rotate the cosine palette every few beats."
  },
  {
    slug: "mandelbulb-march",
    name: "Mandelbulb March",
    category: "raymarching-sdf",
    level: "advanced",
    description:
      "A raymarched 3D Mandelbulb fractal rendered entirely in one GLSL TOP. The classic distance estimator is marched per pixel against a slowly orbiting camera; orbit-trap values captured during iteration tint the surface, and soft shadows, a fresnel rim, and a proximity glow give it depth. No input, no feedback - a drop-in hero render, a looping VJ source, or a reference for distance-estimated raymarching.",
    tags: ["raymarching", "sdf", "glsl", "fractal", "3d", "mandelbulb"],
    requires: "none",
    operator_count: 5,
    key_ops: ["glslTOP"],
    want: "A raymarched fractal hero render",
    prompt: "embody the mandelbulb-march specimen into my project",
    evolve:
      "Raise the fractal power slowly and warm the orbit-trap palette."
  }
];

export const fixtureSummaries: SpecimenSummary[] = fixtureSpecimens.map((specimen) => {
  return toFixtureSummary(specimen);
});

export function firstFixtureSpecimen(): FixtureSpecimen {
  const first = fixtureSpecimens[0];
  if (!first) {
    throw new Error("Missing specimen fixtures.");
  }
  return first;
}

export function findFixtureSpecimen(slug: string): FixtureSpecimen | undefined {
  return fixtureSpecimens.find((specimen) => specimen.slug === slug);
}

export function searchFixtureSummaries(query: string): SpecimenSummary[] {
  const terms = query
    .toLowerCase()
    .match(/[a-z0-9_]+/g);

  if (!terms?.length) return [];

  return fixtureSpecimens
    .filter((specimen) => {
      const haystack = [
        specimen.slug,
        specimen.name,
        specimen.category,
        specimen.description,
        specimen.want,
        specimen.prompt,
        specimen.evolve,
        ...specimen.tags,
        ...specimen.key_ops
      ]
        .join(" ")
        .toLowerCase();

      return terms.every((term) => haystack.includes(term));
    })
    .map((specimen) => toFixtureSummary(specimen));
}

export function toFixtureSummary(specimen: FixtureSpecimen): SpecimenSummary {
  return {
    slug: specimen.slug,
    name: specimen.name,
    category: specimen.category,
    level: toLevel(specimen.level),
    description: specimen.description,
    requires: specimen.requires,
    op_count: specimen.operator_count,
    thumbnail_key: "",
    author_handle: "embody.tools",
    tier: "featured",
    likes_count: 0,
    reactions: {},
    views_count: 0,
    copies_count: 0
  };
}

function toLevel(value: string): SpecimenSummary["level"] {
  if (value === "starter" || value === "advanced") return value;
  return "intermediate";
}
