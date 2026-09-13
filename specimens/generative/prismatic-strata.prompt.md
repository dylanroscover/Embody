# Prismatic Strata

Stacked, jagged strata lit from above and wrapped in iridescent fog. A vertical ramp is
displaced by looping 4D noise into mountain-profile layers that morph slowly; a soft,
large-scale pulse of light rolls down through them once per loop; a per-channel
displacement through a fixed curved-space warp splits every edge into orange and azure
fringes and tints whole regions warm or cool; thin light streaks rise along the curved
layers like rain seen through a lens, and a bloom carries the backlit haze. The texture is
a stack of scales: coarse smooth pulses, mid-scale rims, fine layers, web and grain. The
whole thing is a seamless loop. It is the "animated ramp displaced by animated
noise, RGB-offset, streaked, bloomed" recipe that comes up whenever someone asks how the
layered-iridescent-landscape look is made -- rebuilt as a Ramp TOP, three GLSL TOPs, two
Render Select TOPs and a Bloom TOP.

## What it teaches
- **A ramp displaced by noise IS a landscape.** `v = y * Bands + Displace * fbm(...)`; the
  fractional part of `v` is the phase within one stratum and indexes a 1D **Ramp TOP** (lit
  top edge, grey fog, brown shadow under the next rim). Every rim is a level set of `v`, so
  folds in the noise become extra layers for free.
- **Motion lives in a separate, softer scale.** The rims never scroll; a cosine pulse in a
  coordinate that is mostly `y`, bent by very coarse noise, rolls down `Scroll` bands per
  loop and only modulates brightness (`Pulses`, `Pulsegain`). An integer `Scroll` lands
  exactly on itself at the seam. Dragging the hard rims instead reads as stepping bands.
- **Octave-dependent anisotropy.** The fBm stretches only its low octaves horizontally
  (`Stretch`), while the fine octaves stay isotropic: long ridges with jagged
  mountain-profile edges instead of laminar bands.
- **Seamless looping with 4D noise.** The loop phase walks a circle in the noise's zw
  plane (`Evolution` = radius), so frame N meets frame 0 with no crossfade; higher octaves
  scale their circle slowly so the fine detail does not flicker.
- **Multiple render targets in a GLSL TOP.** `glsl_strata` writes three color buffers
  (`layout(location = 1..2)`): the soft strata; a field with the flow vector, caustic web
  and fine layers; and the layer-normal direction field. Two **Render Select TOPs** pull
  buffers 1 and 2 for the later passes.
- **Per-channel displacement = a Displace TOP three times.** `glsl_prism` samples red at
  `1-d`, green at `1`, blue at `1+d` of the same flow field. The field is static in time on
  purpose: a fixed lens the layers scroll through (an evolving one makes the image swim).
  The detail is composited after the split, sampled at the green position, so crisp lines
  never leave green and magenta copies.
- **Streaks that bend with the layers.** The prism pass leaves thin bright vertical
  features in its alpha; `glsl_streaks` walks each seed along the direction field (the
  gradient of the displaced ramp coordinate, the local "up" through the layers) with a
  bounded max-filter, so the rays rise perpendicular to the curved layers, ragged by a
  random length per column, and screens them over the color.

## How it works
1. `ramp_strata` (Ramp TOP, 512x4, keys in its docked table) is the stratum profile,
   indexed by phase.
2. `glsl_strata` (GLSL TOP, 1920x1080 rgba16float, 3 color buffers) builds the field:
   domain-warped fBm on TD's built-in `TDSimplexNoise(vec4)`, the displaced ramp, a soft
   rim (`Edgesoft`), relief from `dFdy(v)`, a bipolar light field (`Glow`: fog on one side,
   shadow on the other), the rolling pulses (`Pulses`, `Pulsegain`, `Scroll`), a regional
   temperature tint (`Tint`), a thin-film hue cycle riding the pulse coordinate
   (`Iridescence`) and fibrous fine grain (`Grain`). Buffer 1 carries the static flow
   vector, a Worley caustic web whose points orbit with the loop phase, and fine contour
   layers. Buffer 2 carries the normalized gradient of a 2-octave version of `v` (smooth),
   the streak direction.
3. `renderselect_disp` selects buffer 1; `renderselect_grad` selects buffer 2.
4. `glsl_prism` displaces the soft strata per channel (`Flow`, `Dispersion`), composites
   the web (`Causticgain`) and layers (`Layergain`), and writes streak seeds to alpha
   (`Streakthreshold`).
5. `glsl_streaks` walks the seeds along the direction field (`Streaklength`, `Streakgain`).
6. `bloom` (Bloom TOP, pre-black level 0.8) adds the haze; `out1` is the output.

Every parameter on the COMP is bound to a shader uniform through the GLSL TOPs' Vectors
page with `parent.Specimen.par.X` expressions; the COMP sets `Parent Shortcut = Specimen`.

## Recreate it
> Build a looping "prismatic strata" generator in TouchDesigner. A Ramp TOP holds one
> stratum profile (warm lit top edge, grey fog, brown shadow). A GLSL TOP displaces a
> vertical ramp coordinate by domain-warped 4D simplex fBm whose low octaves are stretched
> horizontally and whose loop phase walks a circle in the zw plane; fract() of that
> coordinate indexes the ramp, with a soft rim, dFdy relief and a bipolar light field. Roll
> a soft cosine pulse of brightness (bent by very coarse noise) down the frame an integer
> number of times per loop without moving the rims, add a regional warm/cool tint, a small
> thin-film hue cycle riding the pulse, and fibrous fine grain. Write a second color buffer with a static flow vector, a Worley
> caustic web and fine contour layers, and a third with the normalized gradient of the
> displaced coordinate; pull them with Render Select TOPs. A second GLSL TOP displaces the
> strata per channel (red 1-d, green 1, blue 1+d of the flow), composites the web and
> layers after the split, and writes thin bright vertical features to alpha as streak seeds.
> A third GLSL TOP walks those seeds along the direction field both ways with a bounded
> max-filter (random length per column) and screens them over the color. Finish with a
> Bloom TOP on the highlights only and an Out TOP named out1. Expose Scale, Stretch, Bands,
> Displace, Warp, Detail, Relief, Edge Softness, Layers, Loop Seconds, Scroll, Evolution,
> Seed, Flow, Dispersion, Tint, Iridescence, caustic and streak gains, Glow and Bloom as
> custom parameters bound to the uniforms (plus Pulses, Pulse Gain and Fine Grain).

## Tips
- It animates only while `out1` is cooking: view it, render it, or drive it every frame.
- `Loopseconds` sets the period of the seamless loop; `Scroll` (keep it an integer) is how
  many pulses roll down per loop; `Evolution` is how much the rims morph on top of that
  (0 = frozen rims, moving light). Slow it down for ambient use.
- `Flow` bends everything through the fixed warp (the streaks with it); `Dispersion` is the
  share of that warp that separates the channels. `Dispersion` at 0 gives a neutral grey
  version; `Tint` and `Iridescence` carry the color on their own.
- Restyle every layer at once by editing the Ramp TOP's docked keys table (position 0 is
  the lit top of a layer, 1 is the shadow just under the next rim).
- The prism pass shows as transparent in a viewer because its alpha holds the streak
  seeds; that is by design, the output alpha is 1.
- Cost at 1080p on a mid-range GPU: about 3.5 ms, two thirds of it the streak walk.
