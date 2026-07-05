# Commission 04 -- Lumia

**Thomas Wilfred's light-as-medium, unfurling in a vertical void.**

| | |
|---|---|
| Build as | a container COMP at your project root named `lumia` |
| Discipline / difficulty | bounded feedback advection / advanced |
| Aspect / resolution | **9:16 -- 1080x1920 display; sim buffer 720x1280** (16-bit float first; go 32-bit only if the falloff bands; `out1` carries the native size) |
| Settle before capture | ~900 frames (veils must develop before any capture) |
| Envoy muscles shown | Bounded feedback advection, GLSL flow fields, spectral color handling, grading and finishing in the dark |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Thomas Wilfred coined "lumia" -- light itself as an art medium -- and built the
Clavilux instruments (first public recital, 1922) decades before video
synthesis. His Lumia Suite, Op. 158 ran on its own at MoMA for years: slow
veils of colored light unfurling in absolute darkness, folded like silk or
aurora curtains, hues transforming THROUGH the form as it traversed the frame
over tens of seconds. It is the direct ancestor of everything TouchDesigner
does with light.

Build a lumia composition for a vertical window: one, sometimes two, luminous
veils rising and folding through a black void, advected by a slow flow field,
their interiors shifting emerald to ultramarine to violet to ember, with
faint spectral fringing at the fold edges. Glacial majesty with constant
inner shimmer. Ninety percent darkness, ten percent the most beautiful light
you can make.

This is the anti-plasma: where a plasma effect fills the frame edge-to-edge
with pattern, lumia commands the void with one form. The discipline of
emptiness is the piece.

## Study (what to take from Wilfred)

- **Veils, not blobs**: his forms came through shaped apertures -- elongated,
  sheet-like, FOLDED. A lumia form has visible layered structure, like silk
  turning in slow water, never a round soft spot.
- **Color moves through the form**: the leading edge can burn amber while the
  trailing folds cool to ultramarine -- transitions happen across the body of
  the veil, not as a global tint.
- **Glacial time**: forms take tens of seconds to traverse; full cycles run
  minutes. But the interior always shimmers -- there is no dead frame.
- **Absolute darkness as the stage**: the void is not a background, it is the
  larger half of the composition.

## Look Targets (grade each 0-10; ship at 8+)

1. The form reads as a folded VEIL of light -- sheet-like layered structure
   with fold lines -- not a plasma cloud, not smoke soup, not a lava lamp.
2. At any moment 85%+ of the frame sits near-black; one (occasionally two)
   luminous forms command the space.
3. Spectral interior: hue visibly transitions THROUGH the form, and fold
   edges carry subtle RGB fringing -- prismatic, not rainbow-noise.
4. Two time-scales prove out on camera: a 10-second capture pair shows clear
   evolution (inner shimmer, edge movement); a 2-minute pair shows a
   different composition entirely.
5. Blacks are bottomless but the glow falloff into them is long and clean --
   no banding, no hard clip halo, no milky lift.
6. The vertical format is exploited: veils rise and descend the full height;
   a landscape crop would obviously ruin it.
7. Five-minute soak test passes: no whiteout, no die-off, no drift into mud
   (three captures at t0 / t+2min / t+5min all satisfy targets 1-6).

## Anti-Goals (any one violated = not done)

- Plasma/moire density -- if pattern fills the frame, it is dead.
- Round blob sources or symmetrical mandalas -- veil forms only.
- Fast motion of any kind. Nothing crosses the frame in under ~15 seconds.
- More than 2-3 hues present simultaneously; no rainbow cycling.
- Reaction-diffusion-style texture, cellular detail, or hard edges.
- Unbounded feedback: any monotonic brightness/memory climb across the soak.

## Palette

Void: true near-black with the faintest blue breath `#020208`. The veil walks
a slow palette road over ~2 minutes: emerald `#1FA36B` -> ultramarine
`#2B3FA8` -> violet `#6B3FA0` -> ember amber `#C7651F` -> back. Cores rise
toward warm white only at the brightest folds. Per-source hue offset when two
veils coexist. Finish: wide soft bloom, 1-2% fine grain, imperceptible
vignette.

## Technique spine (latitude allowed; the look is the contract)

- A bounded feedback advection loop at sim resolution: each frame, sample the
  previous frame offset along a slow curl-noise flow field (3D noise with
  z = time, evolving over 30-60s), decay 0.985-0.995, tiny blur, slight
  upward bias (light rises like heat). GLSL TOP for the advection; Reset
  wired; clamp so it cannot run away.
- Injection: one or two elongated soft sources (stretched anisotropic
  gaussians -- slit-shaped, angled) drifting on very slow Lissajous paths,
  hue from the palette walk, energy injected conservatively.
- Spectral fringing: advect R/G/B with slightly different flow offsets (a few
  texels), so fold edges split prismatically.
- Inner shimmer: a small-amplitude, smaller-scale secondary noise modulating
  the advection so the interior never freezes while the form moves glacially.
- After the loop: upscale to 1080x1920, grade (protect the blacks, lift the
  glow mids, deepen saturation in the darks), bloom, grain.
- Keep the loop legible: `feedback_state`, `glsl_advect`, `glsl_inject`,
  named and annotated -- this piece teaches feedback discipline as much as
  beauty.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Pace` | 0.25-2.0 | Master tempo (source drift + flow evolution; 2.0 is still slow) |
| `Huedrift` | 0-1 | Palette-walk rate |
| `Flow` | 0-1 | Flow-field scale/strength (0 = rising straight, 1 = deep folding) |
| `Persistence` | 0-1 | Feedback decay mapping (veil memory length) |
| `Fringe` | 0-1 | Spectral RGB offset amount |

## Notes

- The feedback loop cooks every frame by necessity -- keep the sim buffer at
  720x1280 and gate performance after wiring it, before finishing.
- Capture the hero frame ONLY after driving the full ~900 settle frames; an
  undeveloped lumia is a black rectangle -- that is the cook-demand trap the
  contract warns about, not a broken build.
- The soak test (Look Target 7) is mandatory evidence at the review gate:
  present the t0 / t+2min / t+5min captures.
- Hero frame: a fully developed veil mid-fold, two hues alive in its body,
  vast black around it.
