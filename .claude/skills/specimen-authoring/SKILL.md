---
name: specimen-authoring
description: Workflow and hard-won TD patterns for authoring Embody Specimens (the transparent TDN gallery networks). Load before building or persisting a Specimen.
---

# Specimen Authoring

How to build a Specimen for the Embody Collection -- a transparent, reusable TDN network that demonstrates a TouchDesigner technique. Two specimens set the bar: `reaction-diffusion` (generative, a GPU feedback simulation) and `kaleidoscope` (compositing, a reusable polar-mirror component).

## The bar -- every Specimen must clear it

1. **Clear use** -- a user can finish "I'd use this for ___" (a VJ loop, a drop-in component, a texture/displacement source, a real learning reference).
2. **Non-obvious technique** -- a "how'd they do that?" moment, not a one-TOP drag.
3. **Striking** -- worth opening and exploring.
4. **Drop-in** -- clean input/output, exposed parameters, self-contained, bounded for performance.

Generic noise plus a colorize is NOT a specimen. If a beginner makes it by accident, cut it.

## Workflow (one at a time)

1. **Build in the sandbox** COMP `/specimen_lab/<name>` (NOT inside the Embody COMP -- the toe file, somewhere neutral). Iterate freely there.
2. **Gate performance** (see `performance.md`): baseline `get_project_performance` before, re-check after each heavy step. Feedback loops and GLSL are the usual cost. fps below ~90% of target is a stop condition -- optimize before continuing. Beware: a concurrent `capture_top` or `numpyArray()` stalls the GPU and makes the fps reading dip; take a clean reading without them.
3. **Verify it actually animates and cooks** before believing it -- see "Cook demand" below. Never claim animation from a single forced capture.
4. **Judge the look by actually looking** -- `capture_top` then read the frame; load `visual-aesthetics`. `capture_top` force-cooks, so it can show a frame your live (undemanded) viewer is NOT showing -- a stale viewer and a fresh capture are different frames; that mismatch is real, not a hallucination.
5. **Ask the user to review** before persisting. Persist only when it is genuinely good, animating, and clean.

## Cook demand -- the trap that bit both specimens

A specimen that references time only cooks when its output is demanded (see `td-python.md` Cook Model). Consequences:

- In the sandbox nothing views `out1`, so add a **temporary frame-driver**: an Execute DAT in `/specimen_lab` (OUTSIDE the COMP) with `onFrameStart` cooking the specimen's `out1`. It is not part of the spec; living outside the COMP means it never exports.
- **Verify animation with two frames**: capture, let real seconds pass (a background `sleep`), capture again, compare. If the content differs (beyond a pure rotation), the motion is real. A single frame proves nothing.
- The shipped specimen needs no driver: when a user views `out1`, or the build pipeline cooks `warmup_frames`, the time-dependence runs it.

## Performance: static source + cheap motion

A high-detail generator (domain-warped fBm, a large sim) cannot re-cook every frame at 1280px+. Make it **static** (remove every time reference so it cooks once and caches) and animate a **cheap downstream** op -- drift/rotate/warp the *sample coordinates*, not the source. Confirm `glsl_source.cookedThisFrame == False` and the animated op `== True`. (Reaction-diffusion is the exception: the feedback loop must cook every frame, so keep it bounded -- <=512x512, 32-bit float.)

## Procedural terrain (why fbm looks generic and ridged does not)

- **Ridged multifractal, not plain fbm.** Plain Perlin/Simplex fbm reads as "generic noise"; a ridged multifractal reads as real mountains. The recipe: ridge transform `n = 1 - abs(2*noise - 1)` (sharp ridgelines + V-valleys), `pow(n, sharp)` to sharpen, multifractal weighting `sum += n*amp*prev` (rough peaks, smooth valleys = erosion-like), a **domain warp** (offset the sample coords by another noise) for organic non-grid forms, and offset the height down so valleys drop below the snow line for rock/snow contrast.
- **Keep GEOMETRY octaves LOW (3-4, max freq ~8 at a 128 grid)** and let the MATERIAL bump carry micro-detail. Ridged geometry is rougher than fbm, so extra octaves alias into speckle.
- **In-place 4D morph, not a pan.** Use 3D value noise with the 3rd coord = `time*rate` (a different rate per octave) so the terrain DEFORMS IN PLACE (multi-scale undercurrents). Translating the sample coords by time would pan instead.
- **Geometry-roughness vs material-speckle harmonization.** Rougher (ridged) geometry makes the material's high-frequency terms (slope-dependent snow, per-pixel bump, fine brightness noise) amplify into salt-and-pepper speckle. Fixes: make snow **elevation-based, not slope-based**; **suppress the bump where there is snow** (`bump *= 1 - 0.85*snowMask` -- smooth snow / rough rock); lower the fine-noise frequency and amount.
- **Snow exposure (avoid blowout).** High-albedo snow * bright sun clips to flat white. Dim the sun ON SNOW (`sun *= 1 - 0.55*snowMask`), nudge snow albedo off pure white, and cut the specular.
- **Atmospheric perspective depth.** A saturating exponential fog reads flat / all-or-nothing. Instead normalize distance across the scene (`depth = (dist - near) / range`) and use a POWER curve (`pow(depth, 1.7)*max`) so the foreground stays clear and haze builds with distance; gate any valley mist to distant low areas only.

## GLSL specifics

- **Uniforms** are set on the GLSL TOP via `vec0name` + `vec0valuex/y/z/w` (one vec4), or `const0name`/`const0value`. The values take **expressions** -- bind them to COMP params or time: `gl.par.vec0name='uParams'; gl.par.vec0valuex.expr="parent().par.Segments.eval()"`, `...valuew.expr="absTime.seconds*parent().par.Flowspeed.eval()"`. Declare `uniform vec4 uParams;` and pack four values per vec.
- **Vector uniforms are a parameter SEQUENCE on both glslMAT and glslPOP.** Set the count with `op.seq.vec.numBlocks = N` -- NOT `op.par.vec` (a Sequence-style par whose `.eval()` always reads 0 and will mislead you). Set the type with `vecNtype` (e.g. `'vec4'`). `vecNvaluex/y/z/w` accept expressions; bind them to params.
- **Uniform declaration differs by op type.** In a glslMAT or glslTOP you DO declare `uniform vec4 uShape;` manually. In a **glslPOP compute shader the custom uniforms are AUTO-DECLARED** from the Vectors page -- do NOT write `uniform vec4 uShape;` there or it fails to compile.
- **parent(N) depth when binding params to ops inside a sub-COMP.** Bindings live on ops, but the params live on the specimen COMP. A POP inside `geo` inside the COMP reaches them via `parent(2).par.X`; a MAT directly inside the COMP uses `parent().par.X`. Verify the depth with `parent().path` before trusting it.
- **Direction params (sun / light): expose azimuth + elevation, convert to a UNIT vector in the value expression** -- `x=cos(el)*sin(az), y=sin(el), z=cos(el)*cos(az)` via `math.cos`/`math.radians`. A raw XYZ slider can be zeroed to (0,0,0) and NaN the shader's `normalize()`; az/el is unit by construction.
- Boilerplate (TOP/MAT): `out vec4 fragColor;`, sample inputs `texture(sTD2DInputs[0], vUV.st)`, texel size `uTD2DInfos[0].res.xy`. Bound every `for`/`while` with a constant (GLSL crash rule).
- A generator needs `outputresolution='custom'` + `resolutionw/h`; a simulation buffer needs `format='rgba32float'` or it bands and dies.
- **Every GLSL op you create docks DATs that scatter onto neighbors** -- a GLSL **TOP** docks pixel/compute/info; a GLSL **MAT** docks vertex/pixel/info; a GLSL **POP** docks compute/info. Hug them AND run `get_network_layout` in the SAME `execute_python` that creates the op. NEVER defer to a later cleanup pass: this exact trap has recurred repeatedly because the build loop is heads-down on the shader, and the scattered docks silently pile onto the camera/light/render until someone points at the mess. The Verify step is the only thing that catches it -- run it every time, not just at the end.

## GLSL POP as a geometry generator

A GLSL POP runs a **compute shader** over points -- it is the GPU way to displace / generate geometry (it replaces a stack of fbm noisePOPs). Compute-shader API:

```glsl
void main(){
  const uint id = TDIndex();
  if(id >= TDNumElements()) return;
  vec3 pos = TDIn_P(0, id);   // read input attribute -- NOT TDInPoint_P (that name fails to compile)
  // ...displace pos...
  P[id] = pos;                // write the output array directly -- NOT oTDPoint_P
}
```

- **The output array is undeclared and EMPTY until you set `outputattrs` to `*` (or `P`).** Without it, `P[id]` does not exist. `initoutputattrs=on` seeds the outputs from the inputs.
- The **canonical template is in the docked compute DAT of a freshly created glslPOP** -- it ships with `//P[id] = TDIn_P();`. Read that DAT to confirm the exact API for any build.
- Custom uniforms here are auto-declared (see GLSL specifics above) -- bind `uShape`, `uTime`, etc. to params via the `vec` sequence.

## Naming, layout, output

- Processing ops: `optype_name` (`glsl_colorize`, `feedback_state`); DATs stay role-named (see `td-python.md` Naming).
- Hug docked DATs **at creation, never defer** -- deferring is what leaves them stacked.
- Every specimen terminates in an **Out TOP named `out1`** (the C3 manifest contract `output_op`). Not a Null.
- Annotate **per logical section** (>=400 units between annotation boxes); the annotations are part of the teaching value. Spread the chain if needed to make room.

## Persisting (storage model)

The collection lives at repo-root `specimens/`. Per specimen:

1. The temp driver stays out of the export (it's outside the COMP).
2. `export_network` the COMP -> `specimens/<category>/<slug>.tdn` with `include_dat_content=True` (captures the shaders). The "Failed to track" warning is expected (outside `dev/`). The on-disk `.tdn` is **YAML (TDN v2.0)**: shader/script `dat_content` is a plain string rendered as a YAML literal block scalar (`|`), so GLSL reads top-to-bottom and diffs line-by-line -- do not hand-author it as an array of lines (that was the v1.5 form; v2.0 reverts to a plain string). Keep shader source LF/space-indented, not tab-indented: a tab-bearing string falls back to an ugly double-quoted scalar. Auto-created default docked compute DATs are omitted on export and recreated by TD on import, so an unedited compute companion will not appear in the file. Legacy JSON `.tdn` still import unchanged (json-first parse).
3. Write `specimens/<category>/<slug>.prompt.md` -- what it teaches, how it works, how to recreate it.
4. Add the entry to `specimens/manifest.json` (validate against `specimens/manifest.schema.json`): `slug` is kebab-case (`reaction-diffusion`, not `reaction_diffusion`), `output_op` is `out1`, `requires` is `none` for self-contained specimens, `warmup_frames` covers the thumbnail bake (feedback sims need hundreds-to-thousands; a cheap effect needs a few), `operator_count` = all operators including those nested inside sub-COMPs (annotations not counted).
5. Thumbnails (`thumbnail_path`) are baked by the future build pipeline (`build_specimens.py`), not by hand.

## After persisting

If you discovered a TD behavior that contradicts a rule or skill, **update that rule/skill** (and its shipped template if it has one) -- that is exactly how this skill came to exist.
