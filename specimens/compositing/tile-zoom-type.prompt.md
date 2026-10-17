# Tile Zoom Type

A grid of small letters breathes into one giant letter and back, cycling through
a word with the polarity flipping on every letter. Each cell is a window onto the
same glyph texture: at rest every window shows the whole glyph, at full zoom every
window shows only its own patch of the big letter. Ten TOPs, no CHOPs, no GLSL,
no Python.

## What it teaches
- **Remap TOP as the whole effect**: one glyph texture sampled through an animated
  UV map does the tiling, the zoom, the spin and the glitch.
- **Ramp TOPs as coordinates**: a Ramp with period 1/Grid is the cell-local
  coordinate; a plain Ramp is the screen coordinate. A Ramp composites with its own
  input (Combine with Input), so two Ramps make a two-channel map with no extra op.
- **Channel Mix TOP as a matrix**: a 2x2 rotation written into the mix weights
  spins every cell window about its own center; expressions keep it live.
- **Cross TOP as the zoom**: blending the tiled map into the plain map slides every
  window center from the glyph center to the cell position. Stopping just short of
  1 leaves each window wider than its share, which is where the repeated slivers,
  gaps and arrowheads of the big letter come from. Nothing random.
- **Timing in parameters, not operators**: read-only expression parameters hold the
  eased progress, spin phase and letter index.

## How it works
1. `text_glyph` renders `Letters[Letterindex]` at 60% of a square texture (so a
   spinning window never reaches the edge), swapping font and background colors when
   `Inverted` flips.
2. `ramp_cellx` + `ramp_celly` (period 1/Grid) give the cell-local coordinate.
   `channelmix_rest` turns it into the rest map `0.5 + Glyphscale * R(theta) * local`.
3. `ramp_x` + `ramp_y` give the plain map over the Fit span; `channelmix_delta`
   adds `(Fit/Grid) * (R(theta) - I) * local` so the big-letter crops spin about
   their cell centers too, and the map is exact at whole turns.
4. `cross_uv` blends rest map to plain map by `Zoom` (smootherstep eased). A
   spinning glyph therefore orbits a pivot offset outward from the screen center.
5. `remap_tiles` samples the glyph through the map with Extend Hold; `out1`.

## Parameters
- `Letters`, `Font`, `Typeface` - the word and its face (a light grotesque).
- `Grid` - cells across and down. `Resolution` - output size.
- `Zoomin`, `Hold`, `Zoomout`, `Rest` - segment lengths in seconds (`Period` is
  their sum). Near-zero pauses give a continuous breathe.
- `Glyphscale` - rest window width (1.5 shows the glyph at about 40% of a cell).
- `Holdzoom` - zoom reached at the hold; just under 1 keeps the glitch offsets.
- `Spin` - whole clockwise turns per zoom. `Swapat` - where in the zoom-out the
  letter and polarity cut lands (hidden in the motion).
- `Manual`, `Phase`, `Cycle` - freeze the clock and scrub.

## Recreate it
> Build a letter tile-zoom in TouchDesigner with only TOPs: a Text TOP glyph, two
> period-1/Grid Ramp TOPs as cell-local coordinates and two plain Ramp TOPs as
> screen coordinates, Channel Mix TOPs applying a 2x2 rotation, a Cross TOP that
> blends the tiled map into the plain map by an eased zoom, and a Remap TOP that
> samples the glyph through it, ending in an Out TOP named out1. Expose letters,
> grid, segment durations, hold zoom, spin and the cut point as parameters.

## Tips
- `Holdzoom` 1.0 gives a clean letter; 0.97 a heavier glitch.
- `Spin` 0 removes the rotation; 2 doubles it without changing the timing.
- Set `Manual` on and drag `Phase` to study any moment of the cycle.
