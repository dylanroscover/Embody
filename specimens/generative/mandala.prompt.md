# Thangka Mandala

A Tibetan-thangka mandala drawn entirely from POP geometry: a green field disc inside a
four-colour ring of petals, gold rings, bead and tick rings, lace and garland scrollwork,
eight vase treasures, the palace square with nested colour bands behind one heavy ink wall
and gold corner brackets, T-gates and corner medallions, a dotted lotus seat, two counter-rotating lotus rings, and a rosette, star and
dot at the centre. Every element is a parameterised layer (27 of them) whose outline,
symmetry, colours, nesting and animation are dialled from custom parameters, and the whole
thing breathes: petals tumble and travel in waves, colours cross-fade around the rings,
nested rings spin at different rates, tips flex between round and pointed. A GLSL finish
ages it into a worn cloth painting -- patchy fade toward paper, paper-fibre grunge only in
those patches, a soft vignette and grain. With Loop on, every rate is snapped so the whole
mandala repeats exactly every Loop Length seconds.

## What it teaches
- **One shader for every layer.** Each layer is a static chain (circle -> LayerId/IsFill
  attributes -> Nest copies -> Count copies) merged into ONE line stream and ONE fill stream.
  Three GLSL POPs (line, ink contour, fill) then compute outline, nesting, placement, animation and the
  per-point `Color` / `LineWidth` for all 27 layers at once. Per frame only those three POPs,
  the render and the finish cook: about 1 ms for 40,000 points.
- **Parameters travel in a texture buffer, not as uniforms.** Every layer owns a 64-channel
  Constant CHOP bound to its Layer + Animate pages; `merge_params` joins them in layer order
  and `shuffle_params` swaps channels for samples so the shader reads
  `texelFetch(uP, layer * 64 + j)`. A per-layer GLSL POP with 28 expression uniforms costs
  0.4 ms per cook (TD re-evaluates every expression each cook); this costs nothing.
- **Outlines as a function of angle.** Polygon, star, rosette, scallop, petal, bar and dot
  are all `r(u)` or `(x(s), y(s))` remaps of the same closed line strip, so one Detail
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
- **Exact loops from free-running rates.** In Loop mode every LFO rate is snapped to whole
  cycles per loop, every spin to a whole number of symmetric ring steps (720/Count when
  colours alternate, 360/Cycles under a colour wave), and the hand-drawn wobble walks a
  circle in 4D simplex noise instead of drifting.

## How it works
1. Inside each layer COMP: `circle_line` (closed line strip, Detail points) and
   `circle_fill` (surface fan) -> `attr_line` / `attr_fill` (int `LayerId` = this layer's
   index among the merge inputs, `IsFill`) -> `copy_nest` (Nest copies, `NestId`) ->
   `copy_radial` (Count copies, `CopyId`) -> `out_line` / `out_fill`. `params` (Constant
   CHOP, 64 channels) -> `out_params`.
2. `merge_line` / `merge_fill` (Merge POP, 25 inputs) -> `glsl_line` / `glsl_outline` /
   `glsl_fill` (GLSL POP, `uP` texture buffer from `shuffle_params`, uniforms uTime /
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
   **Wear** (Grunge, Grunge Scale, Coverage, Patch Scale, Fade, Paper, Seed, Vignette,
   Grain). Layers reference the palette by slot number, so recolouring a slot restyles every
   layer that uses it.

## Recreate it
> Build a thangka-style mandala in TouchDesigner from POPs. Make a reusable layer COMP
> whose Layer page chooses an outline (circle, polygon, star, rosette, scallop, petal, bar,
> dot), Radius, Lobes, Amplitude, Sharpness, Length, Width, Detail, radial Count, Nest
> copies with a gap, Rotate, Speed, Pulse, two palette-slot colours with per-copy or
> per-nest alternation, an outline colour, Opacity, Line Width and a hand-drawn Wobble; and
> an Animate page with a travelling Wave, Colour Wave, Tumble, Nest Spin, Wobble Rate and
> amount/rate LFOs on radius, amplitude, sharpness, length, width, nest gap, line width and
> opacity. Keep each layer's geometry static: a closed circle line strip and a surface fan,
> tagged with LayerId and IsFill, duplicated by two Copy POPs that output NestId and CopyId.
> Merge all layers into one line stream and one fill stream and compute everything in two
> GLSL POPs that read the layers' parameters from a Constant CHOP per layer merged and
> shuffled into a texture buffer. Render with an orthographic camera, a Line MAT reading
> the point Color and LineWidth, and a Constant MAT for the fans, over a deep-blue field;
> finish with global saturation and a GLSL wear pass that fades patchy regions toward paper,
> adds paper-fibre grunge only there, a vignette and grain. Add a Loop mode that snaps every
> rate and spin to whole cycles per loop. Compose 27 layers: field disc, dark framing ring, four-colour outer
> petal ring, gold rings, lace border, eight vase treasures, bead and tick rings, palace
> square with nested bands, a heavy ink wall and gold corner brackets (Gap cuts the middle
> of each edge), thin nested lines, gates on top, corner medallions, dotted lotus
> seat, two lotus rings, centre rosette, star and dot. Terminate in an Out TOP named out1.

## Tips
- `Energy` scales every animation amount and `Speed Multiplier` every rate; they are the
  two knobs to tame or excite the whole piece. `Loop` + `Loop Length` make it seamless for
  renders (`Time Mode` = manual with `Manual Time` gives frame-accurate offline stepping).
- Duplicate any layer COMP to add an element: wire its three outputs into `merge_line`,
  `merge_fill` and `merge_params` at the same input index and set its Draw Order.
- Style = Both draws the outline in Line Color over the fill; Line Color 0 keeps the fill
  colours for the outline. `Ink` adds the dark contour behind an element; `Gap` cuts the
  middle of every edge or lobe (0.6 on a square leaves four corner brackets).
- The wear is patchy on purpose: Coverage picks how much of the image is worn, Patch Scale
  how big the patches are, Fade and Grunge how strongly.
- Cost at 1080p: about 1 ms CPU + 0.5 ms GPU per frame; 8x AA is the largest share.
