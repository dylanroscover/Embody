# Flow Field

A quarter of a million particles walk through a slowly changing noise field and
leave their paths behind: fans of fine fibres gather into bright rivers of teal,
cream and magenta. The particles are not geometry while they are being
simulated. Each one is a pixel in a float texture, moved by a GLSL TOP in a
feedback loop, which is how GPU particle systems were built before POPs and
still the approach that scales furthest.

## What it teaches
- **State in a texture**: one RGBA texel per particle holding (x, y, hue, age).
  A 512 x 512 texture is 262,144 particles; the simulation is one pixel shader.
- **Feedback TOP as the loop**: `glsl_seed` scatters the particles once,
  `feedback_state` hands last frame's texture to `glsl_advect`, and its `top`
  parameter names `null_state` to close the loop. Everything in the loop is
  32-bit float with nearest filtering, so no particle bleeds into its neighbour.
- **A heading field, not a velocity field**: noise picks a direction and every
  particle takes one step that way. Headings are not divergence-free, so paths
  converge, which is what gathers the fibres into rivers.
- **Texture back to geometry**: a TOP to POP with First RGBA Contains set to
  Position makes one point per pixel with `P` from RGB. Because blue carries the
  hue, a Lookup Texture POP indexes the palette by `P(2)` to make `Color`.
- **Trails are a second feedback loop**: last frame's picture, dimmed by
  `Trail`, plus this frame's points. Each point is far too dim to see alone;
  the image is hundreds of frames piled up.

## How it works
1. `glsl_seed` writes a random position inside a disc and a random age to every
   texel. It has no time in it, so it cooks once.
2. `feedback_state` -> `glsl_advect` -> `null_state` is the simulation loop.
   The shader reads its own texel, evaluates two octaves of morphing simplex
   noise at the particle's position, turns that into a heading (`Turns` full
   turns per unit of noise), steps `Speed` x frame time along it, stores
   `0.5 + 0.5 sin(heading)` as hue, and advances age. A particle whose age
   passes 1, or that leaves the frame, respawns inside the disc of `Radius`.
3. `topto_points` turns the texture into points; `lookuptexture_color` colours
   them from `ramp_palette`.
4. `render_points` (Render Simple TOP, orthographic, two units wide to match the
   -1 to 1 square) draws them as tiny additive sprites.
5. `feedback_trails` -> `level_fade` (brightness = `Trail`) -> `comp_add` (Add
   with the fresh points) -> `null_trails` is the trail loop.
6. `level_grade` lifts the faint fibres with gamma; `out1` is the output.

## Parameters
- `Grid Resolution` - side of the state texture; count is its square.
- `Scale` - size of the eddies.
- `Speed` - distance per second, in frame halves.
- `Morph` - how fast the field changes; 0 freezes the streamlines.
- `Turns` - how hard the noise bends headings; low is long parallel strands,
  high is tight curls.
- `Radius` - the disc particles are born in; above 1.4 fills the frame.
- `Life` - mean seconds before a particle respawns.
- `Trail` - how much of the last frame survives.
- `Point Size`, `Brightness` - the sprite fed into the trail buffer.
- `Resolution W` / `Resolution H` - output size. The field is square.

## Recreate it
> Build a flow-field particle system in TouchDesigner where the particles live
> in a texture. A GLSL TOP at 512 x 512, 32-bit float RGBA, writes a random
> position in a disc of radius 0.7 to RG, 0.5 to B and a random age to A. Feed
> it to a Feedback TOP whose target is a Null TOP downstream; between them a
> second GLSL TOP (same custom resolution, 32-bit float, nearest filtering) reads its own texel,
> computes a heading = (simplex(p * 1.6, t) + 0.5 * simplex(p * 3.36 + 17, 1.7 t))
> * 2 pi * 0.9 with t = seconds * 0.04, moves the position 0.15 x frame seconds
> along (cos, sin) of it, writes 0.5 + 0.5 sin(heading) to B, adds frame seconds
> / life (3.5 s, varied per texel) to A, and respawns inside a disc of radius
> 0.72 when A passes 1 or the particle leaves -1.15..1.15. Convert the Null with
> a TOP to POP (First RGBA Contains: Position), colour with a Lookup Texture POP
> indexing a five-key teal / aqua / cream / coral / magenta Ramp TOP by P(2), and
> render with an orthographic Render Simple TOP (ortho width 2) and a Point
> Sprite MAT: 1.5 px additive soft dots at 3 percent brightness, 16-bit float,
> 1080 square. Accumulate trails with a second Feedback TOP: Level TOP at
> brightness 0.97 on the feedback, Composite TOP (Add) with the fresh render,
> Null as the feedback target. Finish with a Level TOP at gamma 1.6 and an Out
> TOP named out1. Expose grid resolution, scale, speed, morph, turns, radius,
> life, trail, point size and brightness.

## Tips
- The state texture must stay float and unfiltered. An 8-bit or linearly
  filtered link anywhere in the loop quantises or smears positions.
- Brightness, Trail and Grid Resolution multiply: steady exposure is roughly
  sprite brightness x count / (1 - Trail). Raise one, lower another.
- Set `Radius` above 1.4 and `Life` higher for an all-over field; lower `Turns`
  for long combed strands.
- `Grid Resolution` can be changed while it runs. `glsl_advect` sets the loop's
  resolution from it, and the old state is resampled into the new size. Left
  on Use Input, a feedback loop keeps whatever size it started at.
- It only moves while `out1` is being cooked.
