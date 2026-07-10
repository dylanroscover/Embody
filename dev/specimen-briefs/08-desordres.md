# Commission 08 -- (Des)Ordres

**A Vera Molnar plotter drawing that never stops redrawing itself.**

| | |
|---|---|
| Build as | a container COMP at your project root named `desordres` |
| Discipline / difficulty | scripted composition -> DAT-driven instancing / intermediate |
| Aspect / resolution | **1:1 -- 1080x1080** (`out1` carries the native size) |
| Settle before capture | ~60 frames |
| Envoy muscles shown | Envoy as the algorist -- a seeded Python builder writing composition tables, DAT-fed instancing, and the discipline of AUTHORED randomness; Envoy writes the program that draws the drawing |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Vera Molnar (1924-2023; Budapest-born, Paris-based) was a founder of
generative art. From the late 1950s she was making algorithmic art BY HAND -- her
"machine imaginaire" method, executing rule systems on paper a decade before
she got access to a computer and plotter in 1968. Her plotter series
(Des)Ordres (1974) fills a square grid with nested squares whose corners
tremble: a field of perfect order carrying a deliberate, measured dose of
disorder. "1% de desordre" is the title of a related Molnar work -- the dose
is the art.

Build the drawing as a living plot: an NxN grid of concentric-square cells in
plotter ink on warm paper. A disorder field drifts across the grid, so one
region sits near-perfect while another frays toward collapse; the gradient
migrates over tens of seconds. And every so often the piece RE-PLOTS -- a
pen-sweep redraw with a fresh seed, landing on a new composition that is just
as deliberate as the last.

The badass here is that the artwork is a PROGRAM, and Envoy writes it: a
seeded Python builder authors every segment into Table DATs, instancing turns
the tables into ink. Deterministic, inspectable, re-runnable. Same seed, same
drawing, every time -- exactly Molnar's discipline.

## Study (what to take from Molnar)

- **The cell grammar**: each grid cell holds 4-8 nested concentric squares at
  uniform pen weight. The grid is strict; the squares inside it are where the
  trembling lives.
- **The perturbation grammar**: corner jitter, occasional segment breaks,
  slight rotations -- applied per-vertex and SMALL. Disorder is a surgeon's
  dose, never a scribble.
- **Disorder is FIELDED, not uniform**: the tension of (Des)Ordres is order
  and its dissolution sitting side by side in one frame. Uniform noise
  everywhere reads as texture; a gradient of it reads as an idea.
- **Plotter honesty**: uniform line weight, ink on paper, no fills, no
  shading, no hierarchy of stroke. The pen does not care which line matters.
- **The program is the artwork**: every drawing is one run of a rule system
  with one seed. Nothing is hand-placed; everything is authored.

## Look Targets (grade each 0-10; ship at 8+)

1. Reads as a plotter drawing: even 1-2px-class pen weight everywhere, ink
   sitting ON warm paper -- not vector clip-art, not a shader pattern, not a
   wireframe render.
2. The order -> disorder gradient is legible in EVERY frame: a viewer can
   point at the calm corner and the frayed one without being told.
3. The field migrates: a 30-second capture pair shows the calm region has
   moved to a different part of the grid, while both frames still satisfy
   target 2.
4. The breath is alive but subliminal at the 10-second scale: corners tremble
   a hair between a capture pair; nothing visibly wobbles or swims.
5. A re-plot event is clean and satisfying: cells redraw in a visible sweep
   with pen order (a left-to-right, top-to-bottom feel), landing on a new
   deterministic composition -- caught on a capture pair straddling the event.
6. Ink-and-paper physicality: warm paper with faint tooth, edges a touch
   darker, slight pen texture allowed -- no pure white, no pure black
   clipping, anywhere in the frame.

## Anti-Goals (any one violated = not done)

- Shader/pixel aesthetics: glow, gradients, soft edges, bloom. This is a pen.
- Uniform random jitter across the whole grid -- disorder MUST be a spatial
  field with genuinely calm zones.
- Fast chatter: trembles are slow and near-subliminal; a corner that visibly
  vibrates is a bug, not energy.
- Fills or color washes. Lines only -- the pen never floods a region.
- More than two inks on the paper at once.
- The kitsch "sketchy wobble" filter look -- hand-drawn-ification of clean
  geometry instead of authored per-vertex perturbation.

## Palette

Paper: warm white `#EFEAE0` with faint tooth (very-low-frequency luminance
unevenness), edges a touch darker. Primary ink: blue-black `#1B2440`
(ballpoint class). The `Ink` param sets the SHARE of segments the builder
assigns to a sanguine red `#A03A2E` second pen (0 = all blue-black, 1 = all
sanguine) -- assignment is per segment and seeded; the two inks never blend
into a third. Finish: 1-2% fine grain, nothing else.

## Technique spine (latitude allowed; the look is the contract)

- **The builder**: a Python composition script -- run via `execute_python`, or
  living as a small module DAT inside the COMP -- writes the drawing to Table
  DATs: one row per drawn segment (or per cell), columns for position,
  rotation, length, and per-vertex jitter allowance. Seeded RNG
  (`random.Random(seed)`) so an identical seed produces an identical drawing,
  row for row.
- **The ink**: Geometry COMP instancing rendering segments as thin quads
  under an orthographic camera. Feed it from CHOPs, not the table directly:
  DAT to CHOP -> Math applies the live disorder field to the baked jitter
  columns (an instance parameter reads ONE source, so table and field must
  merge in CHOPs first -- verify the instancing channel mapping against the
  wiki before building). Uniform quad thickness IS the pen weight.
- **The disorder field**: ONE slow low-frequency noise (CHOP or TOP), sampled
  per cell, scales that cell's jitter amplitude. `Migrate` drives the field's
  drift; the builder's jitter columns set each vertex's direction, the field
  sets how far it is allowed to stray.
- **The re-plot**: re-run the builder with the next seed into a second table,
  revealed by a sweeping mask that follows pen order (left-to-right,
  top-to-bottom feel) before it becomes the live table.
- **The paper**: constant + tooth + darker edges composited under the render;
  grain over the top. High MSAA on the Render TOP, or render 2x and downscale
  once, so 1-2px lines stay crisp.
- Keep the chain legible and named: `builder`, `table_plot_a`,
  `table_plot_b`, `instance_segments`, `noise_disorder` -- annotated so a
  reader finds the program before the picture.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Cells` | 8-16 (int) | Grid dimension N |
| `Disorder` | 0-1 | Global disorder ceiling |
| `Migrate` | 0-1 | Disorder-field drift rate |
| `Replot` | 0-1 | Re-plot frequency (0 = never) |
| `Ink` | 0-1 | Share of segments plotted in the sanguine second ink |

## Notes

- The composition table is the heart of the deliverable. Name and annotate
  the chain (builder script -> tables -> instancer) so a reader finds the
  program that draws the drawing -- that IS the Molnar lesson.
- Determinism check on camera: force the same seed twice and capture both
  plots -- the two frames must match segment for segment.
- Spend your iterations on the perturbation grammar: "trembling order" vs
  "messy grid" is a few percent of jitter and where the calm zone sits.
- The square plate deliberately repeats 03's aspect -- for the opposite
  reason: 03 centers a mandala in it; this one rules it into a strict grid.
- Hero frame: mid re-plot -- half the field freshly inked in its new order
  while the old half still frays.
