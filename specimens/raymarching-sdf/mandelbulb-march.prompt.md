# Mandelbulb March

A raymarched 3D Mandelbulb fractal, rendered entirely in one GLSL TOP. The classic
Mandelbulb distance estimator is marched per pixel against a slowly orbiting camera;
orbit-trap values captured during the iteration tint the surface, and soft shadows, a
fresnel rim, and a proximity glow give it depth. No input, no feedback - a drop-in
hero render, a looping VJ source, or a reference for distance-estimated raymarching.

## What it teaches
- **Distance-estimated raymarching** of an implicit 3D surface in a single fragment
  shader: step a ray by the distance bound the estimator returns until it hits.
- The **Mandelbulb distance estimator**: iterate z = z^Power + c in spherical
  coordinates, tracking the running derivative (dr) for the analytic distance bound
  `0.5 * log(r) * r / dr`.
- **Orbit-trap coloring**: record the orbit's closest approach to the axes during
  iteration and map it through a cosine palette - the color comes from the fractal's
  own dynamics, not a texture.
- A no-input generator gets its aspect from `uTDOutputInfo.res` (there is no
  `uTD2DInfos` without an input).
- **GPU safety**: every loop (DE_ITER, MARCH_STEPS, SHADOW_STEPS) is bounded by a
  constant literal, never by a parameter.

## How it works
1. `glsl_mandelbulb` (GLSL TOP, 768x768, rgba16float, no input) runs the whole
   raymarcher in its pixel shader.
2. `mandelbulbDE()` returns the distance to the fractal surface and fills an orbit-trap
   accumulator.
3. `main()` builds an orbiting camera (azimuth from time, plus a gentle vertical bob),
   marches a ray per pixel, and on a hit computes a normal (4 DE taps), a soft shadow,
   a fresnel rim, and orbit-trap color; misses fade to a subtle sky gradient.
4. A proximity glow accumulated along every ray adds the inner light.
5. `out1` is the Out TOP that exposes the COMP's output (opaque, alpha = 1).

Seven parameters drive two vec4 uniforms: **Power** (the bulb exponent), **Surface
Detail** (march epsilon), **Glow**, **Orbit Speed** (camera orbit; 0 freezes a still),
**Hue** (orbit-trap palette), and **Sun Azimuth / Sun Elevation** (converted to a unit
light direction so `normalize()` never sees a zero vector).

## Recreate it
> Build a raymarched 3D Mandelbulb in a single GLSL TOP in TouchDesigner (no input,
> custom output 768x768, rgba16float). In the pixel shader, write a Mandelbulb distance
> estimator (iterate z = z^Power + c in spherical form, power 8, tracking the derivative
> for the distance bound) and march a ray per pixel from an orbiting camera. Shade hits
> with a soft key light + soft shadow, a fresnel rim, and orbit-trap color through a
> cosine palette; add a proximity glow along every ray. Bound every loop with a constant.
> Get the aspect from uTDOutputInfo.res. Expose Power, Surface Detail, Glow, Orbit Speed,
> Hue, Sun Azimuth, and Sun Elevation as Float params bound to two vec4 uniforms, and end
> in an Out TOP named out1.

## Tips
- It animates only while `out1` is cooking (the camera orbits via absTime). Orbit Speed
  0 freezes a still.
- Push **Power** toward 12 for denser lobes, or down to 2-4 for blobby organic forms.
- Crank **Glow** with a low **Sun Elevation** for an ethereal self-lit look; drop Glow
  to 0 for a hard sculptural matte.
- It is a per-frame raymarcher (~13.5 ms GPU at 768 on a strong card). Lower the output
  resolution or Surface Detail on weaker GPUs; raise them for a hero still.
- Swap the spherical z = z^Power formula for a Mandelbox or Juliabulb to explore other
  fractals; feed `out1` into the kaleidoscope specimen for a faceted mandala.
