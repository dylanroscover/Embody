# Commission 06 -- Current

**A Bridget Riley canvas at perceptual threshold -- the eye does the moving.**

| | |
|---|---|
| Build as | a container COMP at your project root named `current` |
| Discipline / difficulty | analytic GLSL precision / intermediate |
| Aspect / resolution | **4:5 -- 1080x1350** (`out1` carries the native size) |
| Settle before capture | ~60 frames |
| Envoy muscles shown | ONE meticulous analytic GLSL TOP -- exact anti-aliasing and a capture-driven perceptual calibration loop; one shader, tuned by eye, carries the whole piece |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Bridget Riley (b. 1931) built the black-and-white period of British op art
from bare geometry -- squares, then parallel lines and curvature: Movement
in Squares (1961), Fall (1963), Current (1964) -- the canvas reproduced on the cover of the
catalogue for The Responsive Eye, MoMA's 1965 exhibition. No image, no
gesture: uniform lines tuned until the eye itself generates shimmer,
afterimage, phantom color. The painting is an instrument played on the
viewer's visual system.

Build a living Current: a full-field fabric of parallel wave lines whose
curvature compresses through a pinch zone, phase drifting SO slowly the
piece never looks like it is animating -- it looks like your eye is doing
it. Every 20-40 seconds a slow breath migrates the compression zone across
the canvas. Nobody catches it moving; everyone who looks away and back
finds it moved.

The badass here is precision. One shader, two colors, and the whole effect
lives inside a couple of pixels of edge. The vibration must come from exact
geometry at exact contrast -- if the renderer contributes any softness, or
any interference of its own, the piece is dead on arrival.

## Study (what to take from Riley)

- **The line grammar of Current and Fall**: uniform-width lines, a curvature
  gradient, compression zones where the frequency seems to double. Every
  line is drafted, none is drawn.
- **Contrast discipline**: ink black on paper white; her grey pairs came
  later -- the `Contrast` param walks toward them, never past them.
- **The shimmer is generated in the VIEWER**, not the canvas -- which is why
  op art fails soft: any blur or mush and the eye has nothing to misfire on.
- **Scale matters**: pitch tuned so roughly 80-120 lines cross the canvas
  width. Too few reads as stripes; too many collapses into grey.
- **Stillness with tension**: the paintings do not move at all. Everything
  the drift does must stay below the threshold where the eye is certain.

## Look Targets (grade each 0-10; ship at 8+)

1. A freeze-frame reads as a Riley op painting -- full-field line fabric,
   curvature gradient, a legible pinch -- not a shader demo, not a stripes
   preset.
2. Lines are razor-crisp at 100% zoom with ZERO rendering moire or aliasing
   beyond the intended perceptual vibration -- judge on actual-size crops of
   the captured frame, never the thumbnail.
3. Motion is near-subliminal: a 10-second capture pair shows a small but
   detectable phase change; a 60-second pair shows the composition clearly
   migrated. Nothing in between ever reads as "animating."
4. The compression zone is legible: line frequency visibly doubles through
   the pinch, and the doubling arrives as a smooth gradient, never a seam.
5. Full contrast range with no clipping halos: ink hits its black, paper its
   white, and the boundary between them carries no ringing or ghost bands.
6. Flat graphic surface -- no lighting, no gradient shading, no depth cues.
   The frame could have been silkscreened.

## Anti-Goals (any one violated = not done)

- Visible waving or flag-ripple. If a stranger says "it is animating" within
  five seconds, it is too fast.
- Rendering aliasing or undersampling moire. The piece IS controlled
  interference -- the renderer must contribute none of its own.
- Soft anti-aliasing wider than ~1.5px: grey mush along every edge turns a
  Riley into a bad scan of one.
- Bloom, glow, vignette, or grain. The surface is clinical; there is no
  finishing pass to hide behind.
- Any chromatic element beyond the warm-neutral paper white. Hue lives only
  in the viewer's afterimage.
- Hand-wobble or noise perturbation on the lines. Riley's curves are exact;
  organic jitter is a different (lesser) piece.

## Palette

Paper white `#F4F2EC`, ink black `#111111`. Nothing else. The `Contrast`
param walks the pair inward toward Riley's grey pairs (`#C9C9C9` paper /
`#3A3A3A` ink) without ever tinting either. No finishing treatment at all --
no grain, no vignette, no bloom; the untouched flat surface IS the finish.

## Motion score

Two motions, both below the threshold of certainty:

- **Drift (the carrier)**: line phase creeps continuously; at `Drift` = 1 a
  line takes roughly 30-60 seconds to travel one line-width. Never faster.
- **Breath (the migration)**: every 20-40 seconds the pinch zone's center
  eases to a new position across the canvas over 10-15 seconds, ease-in and
  ease-out so there is no onset for the eye to lock onto.

The test is negative: no observer should ever catch either motion in the act.

## Technique spine (latitude allowed; the look is the contract)

- ONE GLSL TOP computing analytic distance to a wave-line family. The
  curvature gradient and the frequency doubling come from accumulated /
  integrated phase (integrate the local frequency across the field; do not
  just scale a sine, or the pinch will shear instead of compress).
- Edges via smoothstep anti-aliasing scaled by the local screen-space
  derivative (fwidth-style -- verify the exact GLSL derivative pattern
  against the wiki before building). Hold the edge to the ~1px class.
- If the AA fights the pitch at the pinch's tightest point, render at 2x and
  downscale ONCE -- a single clean resample, never a blur.
- The drift and breath choreography lives in a tiny named CHOP network
  (`drift`, `breath_zone`) exported to shader uniforms, so the near-invisible
  motion stays inspectable in a CHOP viewer even when the render looks still.
- NO feedback, NO post chain. The shader (plus at most the one downscale)
  feeds `out1` directly.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Pitch` | 40-160 | Line count across the canvas width |
| `Amplitude` | 0-1 | Wave depth |
| `Pinch` | 0-1 | Compression-zone strength |
| `Drift` | 0-1 | Phase creep rate (1 is still near-subliminal) |
| `Contrast` | 0-1 | Ink pair: full black/white -> Riley grey pair |

## Notes

- Performance is trivial here -- the risks are aliasing and taste. Spend
  every iteration on calibration, not construction.
- Iterate on 100% crops via `capture_top` and judge like a printer's proof:
  walk the edges of the captured frame at actual size, hunting grey mush and
  stair-stepping. This brief's look loop is calibration, not composition.
- The 10s / 60s capture pairs from Look Target 3 are mandatory review-gate
  evidence -- present both pairs alongside the stills.
- At the review gate, ask the artist for the Riley proof: stare at the pinch
  in a captured frame for twenty seconds. Shimmer, drift, or phantom color is
  a pass; "nice stripes" means keep calibrating. This test is evidence no
  capture can carry -- only the artist's eye can grade it.
- Hero frame: the pinch zone mid-breath, where the frequency doubling is at
  its most electric.
