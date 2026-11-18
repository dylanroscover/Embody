# Ember Plume

A column of sparks rises off a small emitter, leans with the wind, twists, and
breaks into drifting embers as it cools from white through amber to deep red.
It is the plain, built-in way to make a particle system in TouchDesigner: a
Particle POP, two force operators and a colour ramp. No GLSL, no Python, no
CHOPs.

## What it teaches
- **The Particle POP loop**: emitter points go in, the Particle POP births, ages
  and integrates, and its `targetpop` names a Null POP further down the chain so
  last frame's particles come back around. That parameter reference is the whole
  feedback loop; there is no backward wire.
- **Forces are just operators that write `PartForce`**: a Force Radial POP adds a
  constant lift and wind, a Noise POP adds its 3D curl on top. Anything between
  the Particle POP and the feedback Null takes part in the simulation.
- **Drag shapes the motion**: `initdrag` gives each particle air resistance, so
  it leaves fast and settles to a drift near sqrt(force / drag) instead of
  accelerating for its whole life.
- **Colour by age without a shader**: a Math Mix POP divides `PartAge` by
  `PartLifeSpan`, and a Lookup Texture POP reads a Ramp TOP at that value into
  `Color`. The ramp's alpha fades each particle in and out.
- **Additive point sprites**: a Point Sprite MAT with a soft round texture,
  blending on, destination blend One, depth off, then a Bloom TOP.

## How it works
1. `pointgenerator_emit` scatters 2000 points in a flat disc: the places a
   particle can be born.
2. `particle_sim` (Particle POP) births `Birthrate` particles per second with an
   upward start velocity, a lifespan of `Life` plus or minus a quarter, and
   `Drag`. Its pool size follows Birth Rate x Life. `targetpop` is `null_feedback`.
3. `forceradial_lift` writes the global force (`Wind`, `Lift`) plus a slight
   spiral about the vertical axis.
4. `noise_turbulence` adds curl noise to `PartForce`, moving through 4D noise so
   the eddies change over time. Curl is divergence-free, so it stirs the plume
   without bunching it.
5. `null_feedback` closes the loop.
6. Outside the loop, `mathmix_agenorm` makes `AgeNorm` (0 at birth, 1 at death)
   and `lookuptexture_color` turns it into `Color` through `ramp_palette`.
7. `pointsprite_embers` draws `null_color` as additive dots (`ramp_sprite` is the
   dot), `render_embers` (Render Simple TOP) frames it by moving the geometry,
   `bloom_glow` lifts the hot core and `out1` is the output.

## Parameters
- `Birth Rate` - particles born per second.
- `Life` - mean lifetime in seconds; colour runs the whole palette over it.
- `Lift` - upward force.
- `Wind` - sideways force; positive leans right.
- `Turbulence` - curl-noise force amplitude.
- `Drag` - air resistance; higher slows the rise and tightens the plume.
- `Point Size` - sprite diameter in pixels at 1080 wide; scales with resolution.
- `Brightness` - brightness of one sprite.
- `Resolution W` / `Resolution H` - output size. Framed for a square.

## Recreate it
> Build a particle plume in TouchDesigner with POPs and no GLSL. Point Generator
> POP (flat disc, 2000 points) into a Particle POP: birth rate 6000 per second,
> life 4 s with 0.25 variance, initial velocity +1.5 in Y, initial drag 1, jitter
> birth time on, max particles sized to birth rate x life. After it a Force Radial
> POP (radial off, spiral 0.3, global force (0.3, 0.6, 0) with multiplier 1), then a
> Noise POP with Curl 3D on, combine entity Curl 3D, combine and output attribute
> PartForce, amplitude 0.24, 4D time animated, then a Null POP. Set the Particle
> POP's Target to that Null. Off the Null: a Math Mix POP computing PartAge /
> PartLifeSpan into AgeNorm, a Lookup Texture POP indexing a five-key Ramp TOP
> (pale yellow, amber, deep red, alpha 0 at both ends) by AgeNorm into Color, a
> Null. Render it with a Render Simple TOP using a Point Sprite MAT (circular Ramp
> as the colour map, blending on, destination blend One, depth test and depth
> write off) at 1080 square in 16-bit float, add a Bloom TOP, end in an Out TOP
> named out1. Expose birth rate, life, lift, wind, turbulence, drag, point size
> and brightness.

## Tips
- `lifevariance` is a fraction of `life`, not seconds: 1 means lifespans from 0
  to double.
- The Force Radial POP's global force does nothing until `globforcemult` is above
  0; it ships at 0.
- Curl noise is far stronger than its amplitude suggests. Start near 0.2.
- The Ramp TOP type for a soft dot is `circular` with Extend Left on Hold;
  `radial` is the angular sweep.
- It only moves while `out1` is being cooked: view it, wire it on, or render it.
