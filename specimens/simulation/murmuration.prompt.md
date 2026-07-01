# Murmuration

A dense GPU particle swarm that behaves like a starling murmuration at dusk -
cohering, separating, aligning, and flowing around a slow invisible attractor,
with curl-noise wander. A self-contained, purely-procedural, headless-capable
flocking simulation that ends in a single luminous output TOP.

## What it teaches
- A **POP feedback particle system**: the Particle POP births + integrates, and
  its `targetpop` points at a downstream Null POP so last frame's state feeds the
  next - the GPU sim loop.
- **True per-neighbor Reynolds flocking on the GPU**: a Neighbor POP emits a
  per-point neighbor INDEX list (`Nebr`), and a GLSL POP compute shader *iterates
  those real neighbors* (`TDIn_Nebr(0,id,i)` -> random-access `TDIn_P(0,nIdx)`) to
  compute cohesion, alignment, and inverse-square separation directly. This is the
  key to even spacing: a centroid-only force cancels inside a symmetric clump,
  but an inverse-square per-neighbor push is dominated by the closest neighbor and
  never cancels.
- **Forces live INSIDE the feedback loop** (anything writing `PartForce` must sit
  between the Particle POP and the feedback Null); topology-changing / render ops
  live OUTSIDE it.
- **Rendering a POP as additive point sprites**: a side-branch GLSL POP writes a
  speed-mapped `Color` and a `PointScale`; a Point Sprite MAT (additive blend, a
  soft round sprite texture) renders it; Bloom adds the glow.
- Binding **GLSL POP uniforms to COMP parameters** (a `vec` sequence whose
  components are expressions reading `parent().par.X`).

## How it works
1. `source_points` (Sphere POP) seeds start positions; `particle_pop` (Particle
   POP, timeintegration ON) births + integrates them. Its `targetpop` is the
   relative ref `null_sim`.
2. `neighbor_analysis` (Neighbor POP, `nebroutput = nebr`) emits each point's
   neighbor index list `Nebr` (up to 16 within radius 0.2) plus `NumNebrs`.
3. `glsl_flock` (GLSL POP compute) iterates the neighbor list and sums:
   **cohesion** toward the neighbor centroid, **alignment** toward the mean
   neighbor velocity, and **inverse-square separation** (`0.03/dist^2`, capped)
   away from each neighbor; then a slow **moving attractor** (radial pull +
   tangential `spiral`), **curl-noise** wander, a force clamp, a soft
   **containment** sphere (`Bodyradius`) and linear **drag**. It writes
   `PartForce`; `null_sim` feeds the result back to the Particle POP.
4. `glsl_color` (side branch) maps `length(PartVel)` to a 3-stop dusk ramp
   (deep blue-violet / ice-cyan / warm amber) and a `PointScale`.
5. `mat_sprite` (Point Sprite MAT, additive, soft round `sprite_dot` texture)
   renders `glsl_color` in `render_swarm` (Render Simple TOP, near-black bg);
   `bloom_glow` adds the luminous halo; `out1` is the output.

The swarm is uniformly separated (no clumps), shows strong local velocity
alignment, and continuously evolves - loose flowing ribbons compress into bright
warm knots and re-open as the attractor wanders. Runs at 5000 particles.

## Parameters
- `Cohesion` - steer toward the local flock centroid (keep low).
- `Alignment` - match the mean heading of neighbors.
- `Separation` - inverse-square repulsion from close neighbors. The anti-blob
  force; without it the swarm loses its even spacing.
- `Attractor` - pull toward the slow wandering roost point.
- `Spiral` - tangential swirl around the attractor (vortex / split-reform).
- `Curl` - curl-noise wander for organic break-up.
- `Body Radius` - soft containment radius (the size of the coherent body).
- `Morph Speed` - overall evolution rate (attractor drift + curl). 0 freezes it.
- `Max Particles` / `Birth Rate` - population controls.

## Recreate it
> Build a GPU flocking swarm ("murmuration") in TouchDesigner with POPs. Sphere POP
> -> Particle POP (timeintegration on, targetpop = a downstream Null POP for the
> feedback loop). Between them put a Neighbor POP in `nebr` mode (it outputs a
> per-point neighbor index list `Nebr` + `NumNebrs`) feeding a GLSL POP compute
> shader that iterates the neighbor list - read each neighbor's P and PartVel via
> `TDIn_P(0, TDIn_Nebr(0,id,i))` - to compute true Reynolds cohesion, alignment,
> and inverse-square separation, plus a slow moving attractor (radial + tangential
> spiral), curl-noise wander, a force clamp, a soft containment sphere and linear
> drag; write PartForce; the Null feeds back. On a side branch off the Null, a GLSL
> POP maps speed to a blue-violet/cyan/amber ramp + PointScale; render with a Point
> Sprite MAT (additive, soft round sprite) in a Render Simple TOP on near-black,
> add a Bloom TOP, end in an Out TOP named out1. Expose Cohesion, Alignment,
> Separation, Attractor, Spiral, Curl, Body Radius, Morph Speed, Max Particles and
> Birth Rate, bound to the GLSL POP uniforms.

## Tips
- `Separation` is the anti-blob force: lower it and the swarm clumps; raise it and
  it spreads into a lacy, evenly-spaced cloud.
- A *true per-neighbor* separation (inverse-square) is essential - a centroid-only
  push cancels inside a symmetric clump and cannot break it apart.
- `Attractor` + `Spiral` give the swarm somewhere to flow; `Morph Speed = 0`
  freezes it into a single deterministic frame.
- It only evolves while `out1` is cooking - view it, render it, or drive it.
