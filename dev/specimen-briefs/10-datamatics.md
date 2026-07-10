# Commission 10 -- Datamatics

**A Ryoji Ikeda wall: data as raw material, monochrome, frame-exact.**

| | |
|---|---|
| Build as | a container COMP at your project root named `datamatics` |
| Discipline / difficulty | data-driven compositing on a strict clock / intermediate |
| Aspect / resolution | **64:27 ultrawide (the 21:9 monitor class) -- 2560x1080** (`out1` carries the native size) |
| Settle before capture | ~30 frames |
| Envoy muscles shown | DATs as visual material -- data tables rendered into razor-precise TOP compositions on a frame-accurate clock, ultra-wide zone layout; Envoy art-directs DATA, not just pixels |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Ryoji Ikeda (b. 1966), composer and visual artist of the data sublime, works
in raw data itself: datamatics (2006-), test pattern (2008-), and data-verse
(premiered at the 2019 Venice Biennale) render genome strings, astronomical
catalogs, and bare binary as monochrome fields of overwhelming precision,
projected at architectural scale. No metaphor, no illustration -- the data.

Build a data wall: an ultra-wide black field ruled into zones by hairline
verticals. Columns of figures too fine to read stream at locked constant
rates; one huge, slow, mathematically clean sine band sweeps the full width;
1px rules and solid bars cut in and out on a strict clock. Rarely, the whole
wall inverts to white for a held beat -- rarer still, a single thin
signal-red element appears (OUR accent, not an Ikeda quotation; say so at
the review gate).

The badass here is exactness: no glow, no ease, no grain -- every pixel on
the grid or absent, every event landing on the frame the clock names. Where
Overture (02) swings, this piece quantizes -- the same rhythm muscles,
opposite discipline. The sublime is precision multiplied by scale.

## Study (what to take from Ikeda)

- **Data as texture**: glyph columns rendered at 6-9px, legible as MATERIAL,
  never as words -- the eye reads flow, density, and rate, not characters.
- **The monochrome law**: black is the ground state and the dominant mass;
  white is an event. Full-field white is the loudest thing the piece can say.
- **Structure from rules and bars**: hairline verticals rule the wall into
  zones; hard-edged solid bars punctuate it. Everything aligns to an exact
  pixel grid -- a half-pixel is a wrong pixel.
- **Movement is linear or stepped**: constant-rate streams, instant cuts,
  stepped state changes. If anything accelerates smoothly, it is wrong.
- **The rhetoric of scale**: one enormous element (the sine band) against
  thousands of tiny ones (the figures) -- the contrast IS the drama.

## Look Targets (grade each 0-10; ship at 8+)

1. Any freeze-frame reads as Ikeda: monochrome, gridded, data-dense zones,
   vast negative black -- not a screensaver, not a dashboard.
2. The composition is frame-exact: cuts land on the clock, streams move at
   locked constant rates, and nothing eases, drifts, or wobbles, ever.
3. Data texture quality: glyph columns crisp at 100% zoom, fine enough that
   the eye reads flow rather than characters -- no blur, no shimmer.
4. The sine band is majestic: full 2560px width, a clean 1-2px trace, one
   traversal taking 20-60 seconds -- the one slow giant among the fast small.
5. Events are rare and exact: a full-field inversion holds for a fixed frame
   count and returns on the beat; captures during the hold are identical.
6. The red signal element is genuinely rare: alive well under 5% of the
   time, never during an inversion, never more than one at once.
7. The ultra-wide sweep is structural: a 16:9 center crop of any capture
   would obviously amputate the composition.

## Anti-Goals (any one violated = not done)

- Any color beyond black, white, and the ONE signal red.
- Glow, bloom, blur, grain, or vignette -- clinically clean pixels only.
- Eased or organic motion. Constant rates and instant steps only.
- Strobing: full-field luminance flips stay rare and held -- never more than
  one flip in any 2-second window. Hard photosensitivity cap: no flashing
  above 3 Hz, ever, at any parameter setting.
- Matrix-rain / hacker-terminal kitsch: no green, no trailing glyph fades.
- Readable words. The streams carry figures -- hex, binary, decimal -- never
  English strings.

## Palette

True black `#000000`. True white `#FFFFFF`. Signal red `#E60012` class, for
the one rare element only. Nothing else -- the discipline IS the palette. In
deliberate contrast with every other commission in this set, the blacks and
whites CLIP by design. No grain, no grade, no bloom; the finishing treatment
is the absence of one.

## The clock (frame-exact; this is the piece's law)

- One master clock at `Clock` BPM; every event quantizes to it -- zone states
  switch on beats, inversions and cuts on bar boundaries, to the exact frame.
- Streams never stop and never vary rate mid-state: each zone's scroll rate
  is a locked constant, stepped only when its state switches.
- An inversion holds a fixed, clock-derived frame count, then returns. The
  3 Hz cap bounds `Invert` at its maximum -- enforce it in the network.
- The sine band ignores the clock: one continuous constant-rate traversal,
  the only element on its own time.

## Technique spine (latitude allowed; the look is the contract)

- **Master clock**: a Beat or Timer CHOP driving a step sequencer built from
  Count/Pattern/Logic CHOPs that switches zone states; channels named like a
  score (`clock_bar`, `zone3_state`, `invert_hold`). The clock network is the
  conductor -- keep it inspectable, annotated, front and center.
- **Data material**: Python/Table DATs generating figure streams (hex,
  binary, decimal), rendered via Text TOPs into tileable column textures --
  a Text TOP is correct here; this is compositing material, not panel UI --
  scrolled by Transform TOPs at locked constant rates -- in INTEGER pixels
  per frame (derive each rate from the clock), or with nearest filtering on
  the transform, so the 6-9px glyphs never bilinear-smear. Regenerate table
  content on zone-state switches, never per frame.
- **Structure**: Rectangle/Constant TOPs for the hairline verticals and solid
  bars, composited over a black base -- snap every edge to integer pixels at
  2560x1080.
- **The sine band**: one small GLSL TOP drawing an analytic 1-2px sine
  trace, its phase a named `sweep_phase` ramp in the clock CHOP network
  (exported uniform, deliberately unquantized) -- the one anti-aliased
  element on a wall where everything else snaps to the pixel grid.
- **Events**: the inversion as a final Level-stage invert gated by the clock
  (verify the exact op set against the wiki before building); the red element
  as a tiny masked Constant layer switched by the sequencer.
- Every zone is a named, annotated branch; the wall should read in the
  network editor the way it reads on screen -- ruled, exact, zoned.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Clock` | 60-150 | Master tempo (BPM class) |
| `Density` | 0-1 | Stream and zone fill |
| `Sweep` | 0-1 | Sine-band traversal rate |
| `Invert` | 0-1 | Inversion frequency (0 = never; max respects the 3 Hz cap) |
| `Signal` | 0-1 | Red-element probability |

## Notes

- The craft risk is sloppiness, not cost: the build is GPU-trivial, and pixel
  alignment, locked constant rates, and clock exactness are the whole game.
- Verify frame-exactness on camera: two captures one clock-bar apart must
  show a state change; a capture pair 1 second apart during a hold must be
  pixel-identical.
- Verify the ultra-wide argument: center-crop a capture to 16:9 and confirm
  the composition visibly loses limbs.
- Hero frame: mid-sweep -- the sine band crossing a dense zone, one red
  signal element alive far off-axis.
