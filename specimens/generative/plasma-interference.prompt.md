# Plasma (Sine Interference)

A flowing GPU plasma. Two sine-wave fields at slightly detuned scales beat against
each other into shimmering moire fringes, a slow rotating domain warp bends the
sample coordinates into liquid motion, and the result is mapped through a cyclic
cosine palette. Self-contained and stateless -- one GLSL TOP, no input, no feedback --
so it is cheap to run and drops in anywhere as a VJ loop, a texture, or a
displacement / mask source.

## What it teaches
- Building a generator entirely in a **GLSL TOP** with no input: a no-input TOP has
  no `uTD2DInfos[0]`, so the output aspect comes from `uTDOutputInfo.res` (which is
  `(1/w, 1/h, w, h)` -- `.z/.w` is the pixel width/height ratio).
- **Interference / beating**: summing two fields at detuned frequencies produces moire
  fringes that no single field has -- the heart of the "plasma" look.
- **Domain warping**: offsetting the sample coordinates by another field turns rigid
  stripes into organic liquid flow.
- The **Inigo-Quilez cosine palette** (`a + b*cos(2pi*(c*t + d))`): a smooth, cyclic
  hue ramp that never bands and loops seamlessly.
- Driving shader uniforms from custom parameters via the GLSL TOP's **Vectors page**
  (`uParams`, `uPalette`), bound to the COMP's params by expression.

## How it works
1. `glsl_plasma` (GLSL TOP, 1280x720, custom output resolution) runs the whole effect
   in its pixel shader; it has no input.
2. `field()` sums a handful of moving plane-waves plus a radial ripple with a drifting
   center -- one animated sine field.
3. Two fields are evaluated at slightly detuned scales (`f1`, `f2`) and a vignette
   frames the frame. The summed value is folded into 0..1 with `0.5 + 0.5*sin(v*PI)`
   (smooth and cyclic), then colored through the cosine palette.
4. A rotating domain warp (`p + warp * vec2(sin(...), cos(...))`) bends the coordinates
   before sampling, giving the liquid swirl.
5. `out1` is the Out TOP that exposes the COMP's output (opaque, alpha = 1).

Six parameters drive the uniforms: **Scale** (spatial frequency), **Speed** (flow rate;
0 freezes a still), **Warp** (domain-warp amount), **Complexity** (detune between the two
fields -> shimmer), **Palette Hue** (rotates the cosine palette through the hue ring,
loops 1.0 -> 0.0), and **Contrast** (palette saturation onto a 0.6 base).

## Recreate it
> Build an animated plasma in a single GLSL TOP in TouchDesigner (no input, custom
> output resolution 1280x720). In the pixel shader, sum a few moving sine plane-waves
> plus a radial ripple into one field, evaluate it at two slightly detuned scales so
> they beat into moire fringes, and bend the sample coordinates with a rotating domain
> warp for liquid flow. Fold the result into 0..1 and color it with an Inigo-Quilez
> cosine palette; add a soft vignette. Get the output aspect from uTDOutputInfo.res
> (a no-input TOP has no uTD2DInfos). Expose Scale, Speed, Warp, Complexity (detune),
> Palette hue, and Contrast as custom Float params bound to the shader uniforms via the
> Vectors page, and end in an Out TOP named out1.

## Tips
- It animates only while `out1` is cooking -- view it, render it, or drive it every
  frame. Speed = 0 holds a still (the TOP keeps cooking; the frame is identical).
- Stateless and cheap: no feedback, no time accumulation, so it never drifts or blows
  out and needs only a few warmup frames before a thumbnail bake.
- Turn **Complexity** up for a busier, more iridescent shimmer; down to 0 for one clean
  plasma. **Warp** at 0 gives sliding stripes; raise it for the liquid look.
- **Palette Hue** loops -- animate or randomize it for a slow color cycle.
