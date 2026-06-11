# Kaleidoscope

A reusable kaleidoscope compositor: it folds any TOP into an N-fold mirrored
mandala that rotates, twists, breathes, and tumbles. Wire your own visual into it,
or use the built-in animated source.

## What it teaches
- Building a **reusable component with an input** (In TOP) plus a fallback source,
  selected by a Switch.
- A **polar-mirror kaleidoscope** in a GLSL TOP (fold the angle into wedges, mirror, resample).
- Exposing GLSL uniforms as **COMP parameters** (vec4 uniforms driven by custom pars).
- Animating cheaply: a **static detailed source** (cooks once, cached) with all motion
  in the cheap mirror shader (rotation + twist + breathing zoom + drifting sample).

## How it works
1. `glsl_source` renders a static, detailed domain-warped fBm field (cooks once);
   `Palette` rotates its hue.
2. `in1` is the COMP's external input. `switch_source` picks `glsl_source` (Demo) or
   `in1` (External) via the `Source` parameter.
3. `glsl_kaleido` folds the chosen image into `Segments` mirrored wedges, then animates
   rotation, an oscillating twist, a breathing zoom, and a drifting sample so the
   content tumbles through the symmetry (speed = `Flow Speed`, scale = `Zoom`).
4. `out1` exposes the result.

The source is 1280x1280; only the cheap mirror shader cooks each frame, so it stays real-time.

## Parameters
- `Segments` - mirror wedge count.
- `Rotation` - base mirror angle.
- `Zoom` - how much of the source each wedge samples.
- `Flow Speed` - animation speed (0 freezes).
- `Palette Hue` - rotates the color palette.
- `Source` - Demo (built-in) or External (your wired input).

## Recreate it
> Build a reusable kaleidoscope COMP in TouchDesigner: an In TOP and a static
> domain-warped fBm GLSL source feeding a Switch (Demo/External), into a GLSL TOP
> that does a polar N-fold mirror with animated rotation/twist/zoom and a drifting
> sample, ending in an Out TOP named out1. Expose Segments, Rotation, Zoom, Flow
> Speed, Palette, and Source as parameters.

## Tips
- Set `Source = External` and wire any TOP into the COMP to kaleidoscope it.
- Low `Segments` (3-6) reads as bold symmetry; high (12+) as a fine mandala.
