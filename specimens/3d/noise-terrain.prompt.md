# Ridged Mountain Terrain

A procedural snow-mountain scene: a flat grid is displaced into ridged alpine
peaks by a GLSL compute shader, shaded by a snow/rock material, lit by a distant
sun, and composited under a procedural sky. Self-contained and fully parametric.

## What it teaches
- Driving **geometry from a GLSL POP** (a compute shader) instead of fbm noisePOPs -
  the displacement is computed per point on the GPU.
- The **ridged-multifractal** mountain recipe - why plain Perlin / Simplex fbm
  reads as "generic noise" and a ridged multifractal reads as real mountains.
- An **in-place 4D-time morph** so the terrain deforms in place (multi-scale
  undercurrents) instead of panning.
- A **snow / rock GLSL MAT** with elevation-based snow, suppressed bump on snow,
  warm-sun + cool-sky lighting, and depth-graded atmospheric haze.
- Binding **GLSL POP and GLSL MAT uniforms** to COMP parameters across sub-COMP depth.

## How it works
1. `grid_terrain` (Grid POP, 128x128 quads) is the flat starting mesh.
2. `glsl_terrain` (GLSL POP compute shader) displaces each point's Y by a ridged
   multifractal. Each octave does the ridge transform `n = 1 - abs(2*noise - 1)`
   (sharp ridgelines and V-valleys), `pow(n, Ridge)` to sharpen, and multifractal
   weighting `sum += n*amp*prev` (rough peaks, smooth valleys - erosion-like). A
   domain warp offsets the sample coords by another noise for organic, non-grid
   forms. Height is offset down `(h-0.35)*scale` so valleys fall below the snow
   line for rock/snow contrast. The noise is 3D value noise whose 3rd coordinate
   is `time*rate` (a different rate per octave), so the terrain morphs in place.
   It reads two auto-declared uniforms: `uShape=(Height, Ridge, Warp, Seed)` and
   `uTime=(time)`, bound to the COMP's params.
3. `facet_normals` (Facet POP, cusp 160) recomputes smooth normals on the
   displaced mesh; `null_geo` carries the render flag.
4. `glsl_snow` (GLSL MAT) shades it: elevation-based snow (the snow line made wavy
   by a low-frequency noise), bump-mapped rock detail that is suppressed on snow
   (smooth snow / rough rock), warm-sun + cool-sky lighting, the sun dimmed on
   snow so high-albedo snow stays below clipping, and a depth-graded atmospheric
   haze (clear foreground building into the distance). Its uniforms
   `uTerrain=(snowLine, bumpStrength, haze)` and `uSunDir` (a unit vector built
   from azimuth / elevation) are bound to params.
5. `render_scene` (Render TOP, 1024x439, 21:9) renders the geo with `cam` and a
   distant sun `light`.
6. `glsl_sky` generates a bright procedural blue sky with clouds; `comp_sky`
   composites the sky under the rendered terrain.
7. `out1` exposes the result.

Geometry octaves are kept low (3) and the material bump carries micro-detail -
ridged geometry is rougher than fbm, so extra octaves alias into speckle. It runs
around 58 fps at the 128x128 grid.

## Parameters
- `Height` - overall vertical scale (master amplitude on the displacement).
- `Ridge` - ridgeline sharpness (higher = knife-edged ridges, deeper valleys).
- `Warp` - domain warp amount (0 = grid-aligned, higher = organic / chaotic).
- `Detail` - surface micro-detail as shader bump strength (rock roughness).
- `Snow Line` - world-Y elevation where snow begins.
- `Sun Azimuth` - sun compass direction.
- `Sun Elevation` - sun height above the horizon.
- `Haze` - atmospheric haze depth.
- `Morph Speed` - terrain morph animation rate (0 freezes).
- `Seed` - random terrain seed.

## Recreate it
> Build a procedural snow-mountain scene in TouchDesigner. Displace a 128x128 Grid
> POP with a GLSL POP compute shader running a ridged multifractal (ridge transform
> n=1-abs(2*noise-1), pow(n,Ridge), multifractal weighting sum+=n*amp*prev, plus a
> domain warp), using 3D value noise whose 3rd coord is time*rate so it morphs in
> place; offset the height down so valleys drop below the snow line. Recompute
> normals with a Facet POP (cusp 160). Shade with a GLSL MAT: elevation-based snow,
> bump suppressed on snow, warm-sun + cool-sky lighting, sun dimmed on snow to avoid
> blowout, and a power-curve distance haze. Render at 1024x439 with a camera and a
> distant sun light, generate a procedural blue sky in a GLSL TOP, composite the sky
> under the terrain, and end in an Out TOP named out1. Expose Height, Ridge, Warp,
> Detail, Snow Line, Sun Azimuth, Sun Elevation, Haze, Morph Speed, and Seed.

## Tips
- Raise `Ridge` and lower `Snow Line` for jagged, snow-capped alpine peaks; lower
  `Ridge` for rounded eroded hills.
- `Warp` is the difference between griddy repetition (0) and organic ridgelines (1+).
- Set `Morph Speed = 0` to freeze a single deterministic frame; `Seed` then picks
  which mountain you get.
- It morphs only while `out1` is cooking - view it, render it, or drive it each frame.
