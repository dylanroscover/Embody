# Thermite and Freon

Two suns dance in deep space like magnets, colliding in slow-motion golden
explosions, while globs of zero-g water cling to the large one, wobble, get flung
off and drift back. Reaction-diffusion flares erupt from the limbs, electric
sparks streak off every collision, and the whole frame is printed through a film
pass. A self-contained GPU simulation, native 4:5, ending in one output TOP.

## What it teaches
- **A stateful simulation in a texture.** The two suns are a two-body physics sim
  integrated in a 6x1 `glsl_orbit` feedback texture (inverse-square pull, a stiff
  spring where the limbs overlap, a breathing swirl, a soft tether, frame walls).
  Every other layer reads that texture with `texelFetch`, so one tiny state drives
  the flow, the flares, the sparks, the water and the sound.
- **Metaball liquid with lens refraction.** 80 drops in a power-law size spread
  live in an 80x3 state texture (`glsl_drops`); `glsl_water` sums compact
  metaball kernels into one field with an analytic, scale-free gradient, and the
  compose turns it into liquid: each drop refracts the suns and flares behind it
  inverted and magnified, with a Schlick rim, a caustic crescent and glints.
- **Hundreds of particles without a particle system.** Spray droplets live on a
  grid, one per cell (`glsl_spray`): each frame a cell gathers whichever
  neighbour's droplet lands in it, so a pixel only ever reads its 2x2 cells.
- **Reaction-diffusion as flares.** A Gray-Scott field (`glsl_life`, multi-pass)
  is fed only at the suns' active limb sectors and starves with distance, so the
  only pattern that can grow is fingers erupting outward; the flow drags them into
  arcs.
- **Aspect-correct world units.** Every shader works in world units (height 1,
  width = `Aspect`) and samples textures in uv, so discs stay round at any frame
  shape; frame-sized textures take their width from `Resolution * Aspect`.
- **An incompressible fluid on the GPU**: semi-Lagrangian advection, curl-noise
  stirring, vorticity confinement and a multi-pass Jacobi pressure solve over a
  log-spiral terrain that endlessly caves in toward a drain.

## How it works
1. `glsl_terrain` -> `glsl_ground` build the world: a band-limited fractal
   heightfield zooming into the drain, lit and occluded, then re-inked near-black
   so it only reads as texture.
2. `glsl_stream` -> `glsl_flow_step` -> `glsl_pressure` -> `glsl_project` solve
   the water and publish a flow field (uv per frame) that everything rides.
3. `glsl_orbit` integrates the suns and three kinematic moons; `glsl_canopy`
   turns them into distance fields (the noisy limbs and their active sectors).
4. `glsl_life` grows the reaction-diffusion flares; `glsl_pigment` carries silt,
   grain trails and the colour explosions released at each impact.
5. `glsl_drops` -> `glsl_water` is the zero-g water; `glsl_spray` the droplets
   thrown off it when the small sun ploughs through, the suns collide, a drop is
   flung, or two drops slam together.
6. `geo_sparks` (GLSL POPs) fires spark chains from each impact into
   `glsl_streaks`, a per-channel decaying buffer (white -> gold -> red).
7. `glsl_compose` prints it all; `glsl_finish` adds the film pass (halation,
   weave, chromatic fringing, flicker, light leak, vignette, grain, dust) into
   `out1`.
8. `sound_events` (optional) drives two Audio Play CHOPs from the suns' and drops'
   state. The audio library is not part of this file and a pasted copy runs with
   the script disabled, so it plays silent.

## Parameters
- **World**: `Collapse`, `Twist`, `Warp`, `Morph` - how the terrain caves in.
- **Flow**: tide, runoff, `Swirl`, `Jets`, `Vorticity`, `Carve` - the fluid.
- **Pigment**: the flares (`Spores`, `RD Steps`, `Pattern Scale`, `Feed Bias`,
  `Kill Bias`, `Size Range`, `Flares`), the suns (`Large Sun`, `Small Sun`,
  `Separation`, `Orbit Speed`, `Bursts`, `Streaks`) and the water (`Water Size`,
  `Water Pull`, `Water Fling`, `Water Spray`).
- **Look**: `Exposure`, terrain light, `Vignette`, `Grain`, `Glow`, `Melt`,
  `Shimmer`, `Halation`, `Imperfections`.
- **Sim**: `Resolution` (frame height), `Aspect` (0.8 = 4:5, 1 = square,
  0.5625 = 9:16), `Sim Resolution`, `Pressure Iterations`.
- **Sound**: `Sound`, `Volume`, `Beds`, `Events`, `Water`.

## Recreate it
> Build a two-body "magnet" simulation of a large and a small sun in a 6x1 GLSL
> feedback texture (positions, velocities, radii, an explosion envelope fired on
> each impact, three kinematic moons). Read it everywhere with texelFetch. Draw the
> suns from distance fields with noisy limbs; grow Gray-Scott reaction-diffusion
> fed only at active limb sectors so it erupts as flares. Simulate an
> incompressible fluid (advection, curl-noise stirring, Jacobi pressure) that
> carries everything. Add 80 zero-g water drops in an 80x3 state texture, pulled
> to the large sun with overdamped drag, flung off now and then; render them as
> one metaball field with an analytic gradient and shade it as liquid that
> refracts what is behind it. Throw spray droplets on a one-per-cell grid. Fire
> GLSL POP spark chains from each collision into a decaying streak buffer. Work in
> world units (height 1, width = aspect) so every disc stays round. Print it all
> near-black with a film finish and end in an Out TOP named out1.

## Tips
- It runs at 1728x2160 by default; lower `Resolution` for live tweaking and raise
  it again for a render.
- `Aspect` reframes the whole piece without stretching anything.
- `Water Size` scales every drop and droplet together; `Water Spray` 0 turns the
  spray off.
- It only evolves while `out1` is cooking - view it, render it, or drive it.
