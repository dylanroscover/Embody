# Strange Attractor

Two hundred thousand points trace the Thomas attractor, seen down its three-fold
axis: strands leave 48 emitters, stretch into ribbons, fold over each other and
fray as chaos pulls neighbours apart. There is no Particle POP in here. The
simulation is twenty lines of GLSL inside a Feedback POP loop, which is the
pattern to reach for when the motion you want is your own maths.

## What it teaches
- **A Feedback POP is a particle system with nothing built in**: it hands last
  frame's points to whatever follows, and its `targetpop` names the Null that
  closes the loop. You own the integrator.
- **A GLSL POP as the integrator**: one compute-shader invocation per point reads
  `TDIn_P(0, id)`, takes a fourth-order Runge-Kutta step along the flow and
  writes `P[id]`. Position is the entire state: no velocity, no force, no mass.
- **Stateless age**: a point's age is computed from time and its id
  (`fract((seconds + offset) / life)`), so recycling points through an emitter
  needs no Age attribute carried around the loop, and a second shader can
  recompute the same age for colour.
- **Keep the look out of the loop**: `glsl_color` sits on a branch off the Null
  and creates `Color` there, so nothing about the picture feeds back into the
  state.
- **Density as brightness**: two-pixel additive sprites, dim enough that only
  stacked ribbons read bright.

## How it works
1. `pointgenerator_seed` makes `Count` points in a box. It only matters on the
   first frame; after that the loop owns them.
2. `feedback_state` (Feedback POP, `targetpop` = `null_state`) passes the seed
   through once, then last frame's `null_state` forever.
3. `glsl_integrate` steps each point along the Thomas system
   `(sin y - b x, sin z - b y, sin x - b z)` with RK4. `Speed` scales the step,
   `Damping` is b. When a point's stateless age wraps, or it escapes, it is
   moved to its emitter: one of `Strands` points on a sphere, chosen by id,
   jittered by `Spread`. The emitters circle the (1,1,1) axis at `Drift`, which
   is the axis the system is symmetric about, so strands sweep into ribbons.
4. `null_state` closes the loop.
5. `glsl_color` recomputes age and writes `Color`: near-white at birth, azure,
   then violet, fading out over the last quarter of life.
6. `pointsprite_points` and `render_attractor` (Render Simple TOP) draw
   `null_render` looking down the three-fold axis with a few degrees of sway;
   `bloom_glow` and `out1` finish it.

## Parameters
- `Count` - number of points (fixed population). Read when the loop starts:
  after changing it, pulse Initialize then Start on `feedback_state`.
- `Speed` - attractor time per second.
- `Damping` - the Thomas constant b. Up to about 0.21 the flow is chaotic, and
  lower values make a larger, looser tangle (near 0.1 it outgrows the frame).
  Between about 0.21 and 0.33 everything settles onto two closed loops; above
  that it spirals into two points.
- `Strands` - number of emitters.
- `Life` - mean seconds before a point is recycled.
- `Spread` - emitter radius; small gives threads that fray, large gives a cloud.
- `Drift` - how fast the emitters circle the symmetry axis.
- `Point Size`, `Brightness` - the sprite.
- `Resolution W` / `Resolution H` - output size. Framed for a square.

## Recreate it
> Build a strange attractor in TouchDesigner as a custom GPU particle system, with
> no Particle POP. Point Generator POP (box, 200000 points) into a Feedback POP
> whose Target is a Null POP downstream. Between them a GLSL POP with Output
> Attributes P whose compute shader reads TDIn_P(0, id), takes one RK4 step of the
> Thomas system (dx = sin y - b x, dy = sin z - b y, dz = sin x - b z, b = 0.208186)
> with step = frame seconds x 4, and writes P[id]. Give each point a lifespan of
> about 14 s varied by a hash of its id and compute its age statelessly as
> fract((seconds + offset) / life); when it wraps, move the point to one of 48
> emitters placed on a sphere of radius 2.6 by hashing (id mod 48), rotated about
> the (1,1,1) axis by 0.05 rad/s, plus a 0.04 jitter. Off the Null, a second GLSL
> POP creates Color from the same age (white to azure to violet, fading out).
> Render with a Render Simple TOP and a Point Sprite MAT (2 px additive soft dots)
> looking down (1,1,1), 1080 square, 16-bit float, Bloom TOP, Out TOP named out1.
> Expose count, speed, damping, strands, life, spread, drift, point size and
> brightness.

## Tips
- In a GLSL POP the uniforms on the Vectors page are declared for you;
  declaring them again in the shader is a compile error.
- List only what the shader writes in Output Attributes (`P` here). A listed
  attribute the shader does not write can come out as zeros.
- The Feedback POP starts running on its own when the network is loaded or
  pasted. It ignores a change in the seed's point count until you pulse
  Initialize then Start on it.
- Swap `flow()` for any other system (Lorenz, Aizawa, Halvorsen) and adjust the
  emitter radius and camera distance to its size.
- It only moves while `out1` is being cooked.
