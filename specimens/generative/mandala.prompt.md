# Serenity and Beauty

A Tibetan-thangka mandala drawn entirely from POP geometry: a green field disc, veined with
concentric rippling rosette lines that breathe and drift together, inside a
four-colour ring of petals, a thin candy-cane ring of alternating palette blocks turning slowly
outside the gold rings, bead and tick rings, lace and garland scrollwork,
eight vase treasures, a bold ink cross whose four arms run out from beneath the palace to the
cardinal vases (the scrollwork grows over it), the palace square with nested colour bands behind one heavy ink wall
and gold corner brackets, T-gates and corner medallions, a dotted lotus seat, two counter-rotating lotus rings, and a rosette, star and
dot at the centre. Every element is a parameterised layer (32 of them) whose outline,
symmetry, colours, nesting and animation are dialled from custom parameters, and the whole
thing breathes: petals tumble and travel in waves, colours cross-fade around the rings,
nested rings spin at different rates, tips flex between round and pointed. A GLSL finish
ages it into a worn cloth painting -- patchy fade toward paper, paper-fibre grunge only in
those patches, a soft vignette, grain and sparse shimmering specks of light scattered at random and
clumped by the same smooth noise (Sparkle Density, Clumping, Shimmer on the Wear page). With Loop on, every rate is snapped so the whole
mandala repeats exactly every Loop Length seconds.

## What it teaches
- **One master layer, 31 clones.** `field_disc` holds the layer network; every other layer is a
  clone of it (Clone + Enable Cloning) that differs only in its Layer / Animate values, so an edit
  to the master reaches all 32 and the TDXN writes a clone as its values, not its children.
- **One shader for every layer.** Each layer is a static chain (circle -> LayerId/IsFill
  attributes -> Nest copies -> Count copies) merged into ONE line stream and ONE fill stream.
  Three GLSL POPs (line, ink contour, fill) sharing ONE compute DAT then compute outline, nesting, placement,
  animation and the per-point `Color` / `LineWidth` for all 32 layers at once. Per frame only those three POPs,
  the render and the finish cook: about 1 ms for 40,000 points.
- **Parameters travel in a texture buffer, not as uniforms.** Every layer owns one Parameter
  CHOP that emits its Layer + Animate pages as 53 channels in page order (menus as indices);
  `merge_params` joins them in layer order and `shuffle_params` swaps channels for samples so
  the shader reads `texelFetch(uP, layer * 53 + j)`. The palette and master seed ride the same
  way (`params_palette` -> `shuffle_palette` -> `uPal`), so a layer stores a palette slot, not a
  colour. A per-layer GLSL POP with 28 expression uniforms costs 0.4 ms per cook (TD
  re-evaluates every expression each cook); this costs nothing, and the Parameter CHOP is
  three lines of TDXN where a Constant CHOP of bound expressions was 150.
- **Outlines as a function of angle.** Polygon, star, rosette, scallop, petal, bar, dot and arc
  (a ring segment whose Amplitude slants its ends into candy-cane notches) are all `r(u)` or `(x(s), y(s))` remaps of the same closed line strip, so one Detail
  parameter controls smoothness and the same code serves lines and fills.
- **Fills without triangulation.** The fill circle is a `Circle POP` with Surface
  connectivity (a fan with a centre point); the shader maps the centre to the shape's pivot,
  so every star-shaped outline fills correctly and nothing re-triangulates per frame.
- **Painter's order through depth.** Draw Order lifts each layer toward the orthographic
  camera (`P.z = order * 0.002`); the Line MAT's lift keeps outlines on top of their own
  fills; hidden streams park behind everything with alpha 0.
- **Ink contours for thangka contrast.** A third stream (`glsl_outline`, the same shader in
  ink mode) redraws every element's outline in the Ink palette slot, `Ink` times wider than
  its coloured line, on a geometry COMP nudged between the fills and the coloured lines.
  Fill-only shapes get a dark rim, line elements get a dark backing, and one knob per layer
  (plus the master Ink Width / Ink Color) sets how heavy the brush is.
- **Ornament as a texture inside the render.** `glsl_scroll` draws thangka cloud scrolls
  (domain-warped simplex fBm in polar coordinates, folded into mirrored sectors; two interlocking
  families of minute, densely packed filled lobes, vibrant orange and lime green, each with a thin ink rim, plus
  optional faint squiggle lines -- Scroll Scale 12 and Detail 4 keep them tiny against the whole) masked to the green annulus, and `geo_scroll` shows it on a 2x2 quad with a Constant MAT (premultiplied
  blend, no depth write) at a depth just above the field, ripples and ink cross (Draw Order 2 and below) and below every other element.
  Its radial coordinate lives on a circle in the noise's xy plane, so sliding it with time makes
  every lobe grow outward from the centre, never inward, with no seam and an exact loop (whole
  pattern periods per loop). A second field walks a circle in noise space and bends the domain
  warp, so the lobes writhe and swell in place as they grow instead of sliding like a stamp
  (Organic Morph, Morph Rate).
- **Exact loops from free-running rates.** In Loop mode every LFO rate is snapped to whole
  cycles per loop, every spin to a whole number of symmetric ring steps (720/Count when
  colours alternate, 360/Cycles under a colour wave), and the hand-drawn wobble walks a
  circle in 4D simplex noise instead of drifting.

## How it works
1. Inside each layer COMP: `circle_line` (closed line strip, Detail points) and
   `circle_fill` (surface fan) -> `attr_line` / `attr_fill` (int `LayerId` = this layer's
   index among the merge inputs, `IsFill`) -> `copy_nest` (Nest copies, `NestId`) ->
   `copy_radial` (Count copies, `CopyId`) -> `out_line` / `out_fill`. `params` (Parameter
   CHOP on the layer's custom pages, 53 channels) -> `out_params`.
2. `merge_line` / `merge_fill` (Merge POP, 32 inputs) -> `glsl_line` / `glsl_outline` /
   `glsl_fill` (GLSL POP, one shared compute DAT, `uP` texture buffer from `shuffle_params`,
   `uPal` from `shuffle_palette`, uniforms uTime /
   uEnergy / uGlobalRot / uLoop / uLoopLen / uInkColor / uInkWidth / uIsFill) -> `geo_line`
   and `geo_outline` (Line MAT, per-point Color and LineWidth) and `geo_fill` (Constant MAT
   with point colour).
3. `cam` (orthographic, width 2.3 / Scale) -> `render` (8x AA, Resolution square) ->
   `comp_bg` over `constant_bg` -> `hsv_age` (Saturation) -> `glsl_wear` (inputs: image,
   `noise_fiber` sparse noise, `noise_patch` simplex noise) -> `out1`.
4. Master pages: **Mandala** (Resolution, Scale, Rotate, Speed Multiplier, Energy, Line
   Width, Ink Width, Ink Color, Loop, Loop Length, Fill Opacity, Time Mode, Manual Time,
   Saturation, Background),
   **Palette** (8 slots: vermilion, saffron, gold, teal, cream, indigo, rose, ink) and
   **Wear** (Grunge, Grunge Base, Grunge Scale, Coverage, Patch Scale, Fade, Paper, Seed,
   Vignette, Grain, Sparkle Density / Clumping / Shimmer) and **Scroll** (Scrollwork, Scale,
   Detail, Warp, Line Density, Squiggle Lines, Symmetry, Inner / Outer Radius, Growth Stretch, Growth Speed,
  Organic Morph, Morph Rate,
   Fill Color A / B). Layers reference the palette by slot number, so recolouring a slot restyles every
   layer that uses it.

## Recreate it
> Build a thangka-style mandala in TouchDesigner from POPs. Make a reusable layer COMP
> whose Layer page chooses an outline (circle, polygon, star, rosette, scallop, petal, bar,
> dot, arc), Radius, Lobes, Amplitude, Sharpness, Length, Width, Detail, radial Count, Nest
> copies with a gap, Rotate, Speed, Pulse, two palette-slot colours with per-copy or
> per-nest alternation, an outline colour, Opacity, Line Width and a hand-drawn Wobble; and
> an Animate page with a travelling Wave, Colour Wave, Tumble, Nest Spin, Wobble Rate and
> amount/rate LFOs on radius, amplitude, sharpness, length, width, nest gap, line width and
> opacity. Keep each layer's geometry static: a closed circle line strip and a surface fan,
> tagged with LayerId and IsFill, duplicated by two Copy POPs that output NestId and CopyId.
> Merge all layers into one line stream and one fill stream and compute everything in three
> GLSL POPs sharing one compute DAT that read the layers' parameters from a Parameter CHOP
> per layer merged and shuffled into a texture buffer, with the palette in a second buffer. Render with an orthographic camera, a Line MAT reading
> the point Color and LineWidth, and a Constant MAT for the fans, over a deep-blue field;
> finish with global saturation and a GLSL wear pass that fades patchy regions toward paper,
> adds paper-fibre grunge only there, a vignette, grain and sparse roaming specks of light. Draw
> thangka cloud scrolls in a GLSL TOP (domain-warped simplex fBm in polar coordinates folded
> into mirrored sectors, two interlocking families of filled orange and green lobes with thin ink
> rims, masked to the field annulus, growing outward from the centre by sliding a radial coordinate
> that lives on a circle in noise space) and show it on a textured quad inside the render just above the
> field. Add a Loop mode that snaps every
> rate and spin to whole cycles per loop. Compose 32 layers: field disc, a family of thin rippling nested rosette lines (16 nested
> copies alternating cream and gold, one shared phase so they never cross, breathing amplitude
> and spacing;
> a second family is included switched off), dark framing ring, four-colour outer
> petal ring, gold rings, a thin candy-cane ring (two interleaved layers of sixteen arc segments
> with slanted ends, vermilion/cream and teal/gold, ink-rimmed, turning slowly), lace border, eight vase treasures, a bold ink cross (four square-ended
> bars from the centre to the vases, drawn beneath the scrollwork), bead and tick rings, palace
> square with nested bands, a heavy ink wall and gold corner brackets (Gap cuts the middle
> of each edge), thin nested lines, gates on top, corner medallions, dotted lotus
> seat, two lotus rings, centre rosette, star and dot. Terminate in an Out TOP named out1.

## Tips
- `Energy` scales every animation amount and `Speed Multiplier` every rate; they are the
  two knobs to tame or excite the whole piece. `Loop` + `Loop Length` make it seamless for
  renders (`Time Mode` = manual with `Manual Time` gives frame-accurate offline stepping).
- To add an element, clone `field_disc` (a new Base COMP with Clone = `field_disc`), wire its
  three outputs into `merge_line`, `merge_fill` and `merge_params` at the same input index and
  set its Draw Order; never copy a layer, or the copy stops following the master.
- Style = Both draws the outline in Line Color over the fill; Line Color 0 keeps the fill
  colours for the outline. `Ink` adds the dark contour behind an element; `Gap` cuts the
  middle of every edge or lobe (0.6 on a square leaves four corner brackets).
- Draw Order 2 or lower sits beneath the scrollwork quad; the ink cross uses that so the ornament
  grows over it, and any layer you want overgrown can do the same.
- The wear is patchy on purpose: Coverage picks how much of the image is worn, Patch Scale
  how big the patches are, Fade and Grunge how strongly; Grunge Base is the faint fibre over
  everything. Specks re-roll their spots every shimmer cycle and fade in and out.
- Cost at 1080p: about 1 ms CPU + 0.5 ms GPU per frame; 8x AA is the largest share.
