# Commission 02 -- Overture

**An endless Saul Bass title sequence.**

| | |
|---|---|
| Build as | a container COMP at your project root named `overture` |
| Discipline / difficulty | CHOP-sequenced TOP compositing / intermediate |
| Aspect / resolution | **16:9 -- 1920x1080** (`out1` carries the native size) |
| Settle before capture | ~30 frames |
| Envoy muscles shown | CHOP step-sequencing as an edit, TOP compositing, hard cuts, one disciplined GLSL cameo |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Saul Bass invented the modern title sequence: The Man with the Golden Arm
(1955) -- white bars slicing across black until they resolve into the jagged
arm; Anatomy of a Murder (1959) -- black cut-paper body segments assembling to
an Ellington score; Vertigo (1958) -- spirals plotted on John Whitney's
converted mechanical-computer rig. His grammar: flat saturated grounds, two to
five cut-paper shapes, staccato slides that land DEAD, and cuts on the beat.

Build a title sequence with no film attached -- a generative overture that runs
forever. Bars slide in with swing timing and stop hard. Shapes converge into an
abstract figure, hold a beat, scatter. The whole ground hard-cuts to a new
color. A fine-line spiral blooms and recedes -- the Whitney interlude. Then
bars again, never the same twice.

The badass here is restraint: dead stillness between moves, huge negative
space, and timing so tight you can hear the jazz that isn't playing.

## Study (what to take from Bass)

- **Cut paper, not vectors**: flat shapes with honest edges. No gradients, no
  glow, no depth. One accent color per scene, maximum.
- **The slide-and-stop**: shapes enter at constant velocity or with a 2-4
  frame ease-out, then land DEAD. Zero overshoot. The HOLD after the stop is
  as expressive as the move.
- **Cuts, not transitions**: scene changes are one-frame ground flips. Never
  a crossfade.
- **Syncopation**: motion locks to a beat grid with swing -- events land on
  pushed off-beats, not a metronome.
- **The spiral**: Vertigo's Lissajous/logarithmic spirals -- fine lines,
  mathematically precise, hypnotic, slightly menacing.

## Look Targets (grade each 0-10; ship at 8+)

1. Any freeze-frame could hang as a Bass poster: flat saturated ground, at
   most 5 shapes, one accent color, commanding negative space.
2. Motion is staccato -- slide, HARD stop, dead hold. Holds are as long as the
   moves. Zero bounce, zero drift during holds.
3. The edit has swing: cuts and landings sit on a felt beat grid (90-120 BPM)
   with pushed off-beats -- rhythmic, not metronomic, never mushy.
4. The four movements (BARS, ASSEMBLY, FLOOD, SPIRAL) all occur within any
   90-second watch, sequenced with enough variety that no loop is visible.
5. The spiral interlude is precise and hypnotic: fine 2px-class lines,
   moire-free, rotating with menace, gone within a few bars.
6. Color discipline: never more than 3 colors on screen; grounds are flat and
   saturated with only paper-grain (+-2%) texture allowed.

## Anti-Goals (any one violated = not done)

- Gradients, glow, bloom, blur, or 3D anywhere.
- Elastic/bounce easing, or continuous ambient wander -- if nothing is
  scheduled to move, NOTHING moves.
- More than 5 shapes on screen, or shape clutter that kills the negative space.
- Typography (no fonts, no letterforms -- shape language only).
- Crossfades. Every transition is a cut.
- Pastel or muddy grounds -- Bass grounds are ink-saturated.

## Palette (expose as a 3-way menu)

- `Anatomy`: ground burnt orange `#E8541F`, shapes ink black `#141210`,
  accent bone `#F4EAD8`.
- `GoldenArm`: ground ink black `#141210`, shapes bone `#F4EAD8`, accent
  crimson `#C0322B`.
- `Vertigo`: ground deep crimson `#8C1F24`, shapes ink black `#141210`,
  accent mustard `#E0A32E`.

FLOOD movements may swap ground and shape colors (negative-space inversion)
within the active set.

## Motion score

A scene sequencer cycles four movements, weighted-random, 8-16 beats each at
90-120 BPM:

- **A. BARS** -- 3-7 bars slide in from alternating edges on swing eighths,
  land hard, hold 2-4 beats, exit fast on the downbeat.
- **B. ASSEMBLY** -- bars and wedges converge to center into an abstract totem
  silhouette; one beat of dead stillness; scatter outward in 2 frames.
- **C. FLOOD** -- one-frame ground flip; a single shape inverts (figure/ground
  swap); hold; a second flip resolves.
- **D. SPIRAL** -- the Whitney interlude: a fine-line logarithmic spiral blooms
  from a point, counter-rotates, recedes into black. Rarer than A-C.

Swing law: off-beat events land at 55-62% of the beat interval, not 50%.

## Technique spine (latitude allowed; the look is the contract)

- TOP-first compositing: flat Constant/Rectangle-style shape layers moved by
  Transform TOPs, stacked with Over/Composite, ground behind. (Instanced quads
  under an ortho camera are acceptable if compositing gets unwieldy -- but keep
  the flat, unlit look.)
- The rhythm section is the star: Timer/Beat CHOP -> step patterns
  (Pattern/Count/Logic) -> per-shape position channels shaped by Lag/Filter
  with asymmetric ease (fast in, instant stop) -> exported to the transforms.
  Scene switching via a Count-driven index into Switch TOPs. Name channels
  like an edit sheet (`bar3_slide`, `scene_index`, `flood_flip`).
- The spiral is the one GLSL cameo: a small GLSL TOP drawing a parametric
  logarithmic spiral (bounded loop, analytic anti-aliased line via
  smoothstep on distance-to-curve), rotating; composited flat over the ground.
  No other shaders.
- Finish: +-2% paper grain, nothing else. Edges stay clean at 1080p (analytic
  edges or high AA).

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Tempo` | 80-140 | BPM of the edit grid |
| `Swing` | 0-0.35 | Off-beat push amount |
| `Palette` | menu | `Anatomy` / `GoldenArm` / `Vertigo` |
| `Density` | 0-1 | Shape-count bias within movements |
| `Spiralbias` | 0-1 | Frequency of the SPIRAL movement |

## Notes

- Performance is trivial; the craft risk is timing. Iterate on FEEL: capture
  pairs a few beats apart and check that holds are truly dead and landings
  truly hard.
- Verify no visible repetition across 60s: three captures 20s apart must not
  show the same scene state.
- Hero frame: mid-ASSEMBLY -- the totem half-formed, negative space huge, one
  accent shape in flight.
