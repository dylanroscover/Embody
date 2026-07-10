# Commission 07 -- Scan Processor

**A Vasulka-era elegy: the raster becomes a mountain range of light.**

| | |
|---|---|
| Build as | a container COMP at your project root named `scan_processor` |
| Discipline / difficulty | Rutt/Etra displacement -- cross-family signal flow / advanced |
| Aspect / resolution | **2:1 -- 2048x1024** (`out1` carries the native size) |
| Settle before capture | ~120 frames |
| Envoy muscles shown | The TD-native homage -- a TOP signal crossing families into geometry (Rutt/Etra scanline displacement), luminous line rendering, phosphor persistence, slow camera craft |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

In 1973 Steve Rutt and Bill Etra built the Rutt/Etra Scan Processor, an
analog instrument that deflected a CRT's scanlines with the video signal
itself: brightness became vertical deflection -- elevation -- and any image
became a wireframe landscape of light. Steina and Woody Vasulka, founders of
The Kitchen in New York (1971), made it a language; Woody's C-Trend (1974)
renders street traffic as rolling electron terrain. TouchDesigner's
video-synthesis lineage runs straight through this instrument.

So build the homage natively. A slow generative luminance field -- dunes of
drifting noise -- displaces 90-140 discrete horizontal scanlines into a
phosphor terrain, seen from a low, slowly drifting camera, every line
glowing with CRT persistence. Every 15 to 30 seconds a surge -- an amplitude
spike in the source signal itself -- sweeps through the raster, cresting the
dunes and trailing a fading wake of persistence.

The badass here is the lineage made visible: an image becomes a signal
becomes geometry becomes light, the exact trick the analog instrument
performed with deflection coils, executed with one honest mapping and total
reverence. If it looks decorated, it is wrong; if it looks inevitable, it is
right.

## Study (what to take from the Vasulkas and the Rutt/Etra)

- **Brightness equals elevation is the entire grammar**: one honest mapping,
  no decoration. Everything you see must be explainable as luminance lifted
  into height.
- **The scanline is sacred**: discrete horizontal lines, countable, with
  black between them -- never a solid shaded mesh, never filled faces.
- **Phosphor persistence**: a bright pass leaves a fading trace; the screen
  remembers. Motion writes its own history into the frame.
- **The signal as material**: the Vasulkas treated video as a substance, not
  a picture. The piece should feel ANALOG -- slight line-weight variation,
  soft overglow at peaks, bottomless black -- not vector-crisp CG.
- **C-Trend's temperament**: patient, monumental, a little mournful. Traffic
  became terrain; here, noise becomes dunes. Slow is the point.

## Look Targets (grade each 0-10; ship at 8+)

1. Reads instantly as Rutt/Etra scanline terrain: discrete glowing raster
   lines displaced by an image -- never a wireframe-shaded 3D mesh, never a
   filled surface.
2. Majestic slowness: the dunes evolve over tens of seconds and the camera
   drift is barely perceptible -- a 10-second capture pair shows subtle
   change; a 60-second pair shows a different terrain.
3. The surge reads clearly on a timed capture pair (t / t+5s): a luminous
   front at two distinct positions along the raster, trailing persistence
   behind it.
4. The phosphor look lands: hot line cores, soft halo, decaying trails, and
   a deep black floor between and beyond the lines.
5. Line rendering is clean at 2048 wide: no stairstep sparkle, no z-fighting
   between neighboring lines, no moire shimmer as lines converge.
6. Depth staging exploits the 2:1 frame: near lines bold and widely spaced,
   far lines converging toward a wide, low horizon that dissolves into
   black.

## Anti-Goals (any one violated = not done)

- Tron/synthwave kitsch: no magenta-cyan gradient sky, no sun disc, no
  chrome grid, no retrowave anything.
- A solid shaded terrain mesh -- the moment faces render, the raster is
  dead. Lines only.
- Fast flythrough motion. This is a surveyor's gaze, not a spaceship run.
- Glitch chatter: no datamosh, no jitter, no added scanline-noise effects.
  The instrument was clean; the persistence IS the texture.
- Bloom fog lifting the black. Halo hugs the lines; the void stays void.
- Any text or HUD element.

## Palette

Void black `#030404` -- floor, sky, and the gaps between lines. Lines: a
P31-phosphor class green -- hot core `#B8FFD9` over body `#2FBF71` -- with
the `Phosphor` param walking the whole raster green -> amber (`#FFC96B` core
over `#C77E2F` body) -> blue-white. Peaks rise toward white-hot ONLY at
surge fronts. Finish: soft additive halo on the lines, faint grain, blacks
protected; at most a barely-there vignette -- CRT breath, not an effect.

## Technique spine (latitude allowed; the look is the contract)

- **Source**: a slow drifting noise field at low resolution -- one texel per
  line vertex, e.g. 256x128 -- using the static-source-plus-cheap-motion
  discipline where possible (drift the sample coordinates, keep the heavy
  octaves cached). The surge is a bright ridge added to this field on a
  schedule from a small named CHOP network (`surge_clock`).
- **Geometry**: instanced horizontal line strips, one instance per scanline,
  whose vertices displace vertically in a vertex shader (GLSL MAT) sampling
  the source TOP -- OR the classic conversion path (TOP to CHOP -> CHOP to
  SOP driving line geometry) if it proves cleaner. Verify the exact op set
  against the wiki before building.
- **Render**: additive, with slight line-to-line brightness variation for
  the analog feel. The material follows the geometry path -- a Geometry COMP
  renders with ONE material: on the GLSL MAT path the displacement shader
  also owns the line look; a Line MAT (width/taper -- verify parameters
  against the wiki) applies only to the CHOP -> SOP path. Never plan both.
- **Persistence**: a bounded Feedback TOP AFTER the Render TOP -- previous
  frame multiplied by 0.88-0.95, composited under the fresh frame, Reset
  wired, decay clamped so it cannot run away.
- **Camera**: low and long-lens, just above the nearest lines, drifting
  laterally with a period of 60s+; the horizon sits low in the 2:1 frame.
- **Post**: halo bloom on emissives only, faint grain, a final grade that
  protects the blacks. Name the path so it reads as a signal chain:
  `noise_signal`, `instance_scanlines`, `glsl_displace`,
  `feedback_phosphor`.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Relief` | 0-1 | Displacement height (brightness -> elevation gain) |
| `Evolve` | 0.25-2.0 | Dune evolution rate (2.0 is still slow) |
| `Surge` | 0-1 | Surge-front frequency (0 = becalmed) |
| `Afterglow` | 0-1 | Phosphor decay length |
| `Phosphor` | 0-1 | Hue walk green -> amber -> blue-white |

## Notes

- This piece is the lineage lesson: annotate the signal path so a reader can
  follow image -> signal -> geometry -> light through the named ops
  (`noise_signal` -> `glsl_displace` -> `instance_scanlines` ->
  `feedback_phosphor`).
- Gate performance after the line rig and again after the feedback loop --
  the instanced lines and the persistence buffer are the two costs worth
  measuring.
- Verify the surge on camera with a t / t+5s capture pair, and test both
  ends of `Phosphor` and `Afterglow` before calling them exposed.
- Hero frame: a surge front cresting the tallest dune, persistence trail
  behind it, the far raster dissolving into black.
