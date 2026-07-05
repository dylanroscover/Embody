# Commission 01 -- Point and Line to Plane

**A Kandinsky composition that plays itself.**

| | |
|---|---|
| Build as | a container COMP at your project root named `point_line_plane` |
| Discipline / difficulty | CHOP-driven instancing / intermediate |
| Aspect / resolution | **4:3 -- 1440x1080** (`out1` carries the native size) |
| Settle before capture | ~60 frames |
| Envoy muscles shown | CHOP choreography -> instancing, ortho render, matte finishing |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Wassily Kandinsky was a synesthete: he heard color, painted music, and titled
canvases like scores -- Compositions, Improvisations. His Bauhaus-period
geometry (Composition VIII, 1923; the grammar book Point and Line to Plane,
1926) is the ancestor of every motion-graphics system that followed. It is
begging to move the way he heard it.

Build a living Kandinsky: a geometric composition on warm ivory paper where
every element drifts in slow orbital counterpoint, and every few seconds one
element is STRUCK like an instrument -- a line cluster plucked, a halo rung, an
arc swept. The network itself is the score: named CHOP channels are the voices,
instancing turns them into paint.

This is a painting first. Matte, warm, physical. If it glows, it is wrong.

## Study (what to take from Kandinsky)

- **The element grammar of Composition VIII**: one large anchor circle with a
  halo ring; satellite circles, some concentric; clusters of 5-9 thin parallel
  lines at a shared angle; open arcs; small checkerboard grids; sharp wedges
  and triangles whose overlaps mix like translucent glazes; one long diagonal
  crossing the field.
- **Asymmetric balance**: a big quiet mass (the anchor circle, upper-left
  third) balanced against a busy small-element cluster (lower-right). Diagonal
  energy flows lower-left to upper-right.
- **The ground is a player**: warm ivory paper, not white; slightly aged and
  uneven, darker toward the edges.
- **Tension, not symmetry**: elements point, lean, and aim at each other.
  Nothing is centered. Nothing is random either -- every placement is a note.

## Look Targets (grade each 0-10; ship at 8+)

1. A freeze-frame reads instantly as Bauhaus-period Kandinsky brought to life:
   geometric, musical, warm paper ground -- not clip-art on a beige rectangle.
2. Asymmetric balance holds: one clear anchor mass, a countering busy cluster,
   at least 30% of the canvas as quiet breathing ground.
3. Everything sits matte ON the paper like gouache: no bloom, no glow, no pure
   white or pure black clipping; overlapping shapes mix like translucent
   glazes, not opaque stickers.
4. In any 10-second window: continuous gentle drift everywhere, plus at least
   two percussive events that visibly feel STRUCK (fast attack, slow ring-down).
5. Thin lines and circle rims stay crisp at 100% zoom -- no aliasing shimmer,
   no soft mush.
6. The network reads as a score: choreography channels named like voices
   (`pluck_lines_a`, `ring_halo`, `sweep_arc_2`), annotated per section.

## Anti-Goals (any one violated = not done)

- Random shape soup. The base composition is AUTHORED and fixed; motion
  perturbs it, never scrambles it.
- Screensaver spin. Nothing completes a visible rotation in under a minute.
- Neon, bloom, gradients, lens effects. This is paper and pigment.
- Elastic/bounce easing. Percussive events are pluck-decay envelopes (fast
  attack, ~1.5s release), never springs.
- Elements drifting off-canvas or colliding into mud.

## Palette

Warm ivory ground `#EDE6D4`, aged toward `#D9CFB8` at the edges (very-low-
frequency luminance unevenness, +-3%). Elements: ink black `#1A1714`, crimson
`#B33025`, cobalt `#2B4C8C`, ochre `#D9A441`, dusty rose `#C98A7D`, sage
`#6E8B74`, deep violet `#5C4470`. The anchor circle: near-black violet core,
crimson halo, thin ochre rim. Fine monochrome grain (2-3%) over everything.

## Motion score

Two time-scales, always both:

- **Continuous (the drone)**: circles orbit invisible centers with periods of
  30-120s in harmonic ratios (1:2:3:5); line clusters lean +-4 degrees over a
  minute; arcs creep at degrees-per-minute; the long diagonal breathes.
- **Percussive (the melody)**: every 2-6 seconds ONE event fires, chosen
  round-robin/weighted: a line cluster plucks (offset snap, decay back); a
  circle halo rings (scale 1 -> 1.12 -> 1); an arc sweeps its arc-length open
  or closed; a wedge snaps to a new rotation in one quantized 15-degree step.
  Attack under 100ms, release ~1.5s (Lag CHOP with asymmetric up/down).

The rhythm section is a CHOP network: a master tempo, a beat/trigger layer,
envelope shaping, all exported into the instance channels. A reader should be
able to SEE the music in the CHOP viewers.

## Technique spine (latitude allowed; the look is the contract)

- Orthographic camera + Render TOP. Element classes as instanced Geometry
  COMPs (circles, rings, thin quads for lines, wedges, checker cells), with
  instance translate/rotate/scale/color fed from the choreography CHOPs --
  the score literally becomes geometry. Total instances ~30-50; GPU cost is
  trivial.
- Translucent glaze mixing via material alpha/blend on overlapping wedges.
- Paper: Constant + low-frequency Noise unevenness + vignette, composited
  under the render; grain on top. High MSAA on the Render TOP (or render 2x
  and downscale once) for the crisp-edge target.
- Expression of the beats: Beat/Timer/Pattern/Logic/Trigger/Lag CHOPs. Keep
  every channel named as a voice.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Tempo` | 0.25-2.0 | Master rate: event frequency and drift speed together |
| `Energy` | 0-1 | Strike intensity (pluck distance, ring amplitude) |
| `Drift` | 0-1 | Orbital/lean speed scale (0 freezes the drone, melody continues) |
| `Warmth` | 0-1 | Ground tint cool-parchment -> warm-ivory |
| `Grain` | 0-1 | Paper grain amount |

## Notes

- Performance is trivial here; the craft risks are composition and easing.
  Spend your iterations on placement, palette balance, and strike feel.
- Hero frame: catch a moment mid-strike (a plucked line cluster still
  displaced) so the stillness has tension in it.
