# Commission 05 -- Radiolaria

**A Haeckel plate come alive: a bioluminescent lattice organism in deep water.**

| | |
|---|---|
| Build as | a container COMP at your project root named `radiolaria` |
| Discipline / difficulty | full 3D render pipeline / advanced |
| Aspect / resolution | **3:4 -- 1080x1440** (`out1` carries the native size) |
| Settle before capture | ~120 frames |
| Envoy muscles shown | The full classic render pipeline: SOP/POP geometry, MATs, a real lighting rig, camera craft, DOF, post -- the piece that proves Envoy can direct a scene, not just a shader |

Read the companion `_contract.md` FIRST. It is binding.

## The commission

Ernst Haeckel's Kunstformen der Natur (1899-1904) opens with radiolaria --
single-celled organisms whose silica skeletons are nested geodesic lattices
bristling with radial spines (Circogonia icosahedra, the icosahedron that
evolution drew first). His plates are portrait-format acts of scientific
reverence: the specimen luminous against a dark field, ornate, gothic,
impossibly precise.

Build a living plate: one radiolarian floating in deep water, rotating with
tectonic slowness, nested lattice shells counter-rotating almost
imperceptibly against each other, the whole organism breathing. A warm amber
life-light glows inside the innermost shell; cold cyan bioluminescence rims
the lattice from behind. Every eight to fifteen seconds a luminous pulse
travels outward through the lattice like a nerve signal. Marine snow drifts
past, melted into bokeh by shallow depth of field.

Where a pure-shader 3D scene is a GPU flex, this one is CINEMATOGRAPHY:
geometry, lights, camera, focus. It should look like a frame from a nature
documentary shot two thousand meters down.

## Study (what to take from Haeckel)

- **Nested geodesic shells**: an outer lattice sphere and a smaller inner one,
  concentric, both built from clean polygonal frames with open pores.
- **Radial spines**: long tapered needles radiating from the outer shell's
  vertices -- varied lengths, slight irregularity; the silhouette reads spiky
  and delicate at once.
- **Portrait reverence**: the plate composition -- specimen centered but
  breathing room above, darkness pressing in from the corners.
- **Organic imperfection**: Haeckel idealized, but nothing is machine-perfect;
  vary lengths and angles a few percent everywhere.

## Look Targets (grade each 0-10; ship at 8+)

1. Reads as a Haeckel radiolarian brought to life: nested geodesic lattice +
   tapered radial spines + portrait-plate composition -- not a techno
   wireframe ball.
2. The complementary light story lands in every frame: cold cyan rim from
   behind/above vs warm amber heart inside the inner shell -- both present,
   neither clipping.
3. Real photographic depth: the organism tack-sharp, marine snow melted into
   soft bokeh fore and aft; the DOF is obvious but never gimmicky.
4. Majestic motion: rotation slow enough to feel massive (no full revolution
   inside a minute); the breathing (2-3% scale/spine flex) is perceptible
   within 10 seconds; shells visibly counter-rotate over ~20 seconds.
5. A bioluminescent pulse traverses the lattice every 8-15 seconds -- a
   brightness wave traveling vertex-to-vertex outward, gone in ~2 seconds.
6. Lattice members resolve as clean tapered strands at 1080x1440 -- no
   aliasing crawl, no wireframe shimmer.
7. The water is a place: deep teal-black ground, corners near-black, faint
   suggestion of light from above -- atmosphere, not empty render viewport.

## Anti-Goals (any one violated = not done)

- Tron/hologram/sci-fi vibes: no grid-blue everything, no scanlines, no
  glitch, no HUD energy.
- Fast or wobbly motion; the mass illusion dies above tectonic speed.
- Machine-perfect symmetry: identical spines, uniform glow, zero variance.
- Bloom fog -- glow discipline; the void must stay dark.
- Particle blizzard: marine snow is SPARSE (a few hundred motes, mostly
  defocused).
- Flat lighting or an ambient grey wash.

## Palette

Water: deep teal-black `#02090D`, lifting to `#07333B`-tinged darkness in the
upper light. Lattice: pale bone-silica `#B8C4C0` catching cyan rim light
`#3FD8CE`. Core light: warm amber `#E8A33D` with the innermost glow toward
`#FFD9A0`. Marine snow: dim bone-white motes. Finish: tight bloom on
emissives only, 1-2px chromatic aberration at the frame edges, vignette to
near-black corners, fine grain.

## Technique spine (latitude allowed; the look is the contract)

- **Lattice**: icosphere-frequency polygonal spheres turned into strand
  geometry -- e.g. a subdivided platonic/sphere SOP whose edges become tapered
  tubes (Wireframe-style SOP conversion, or lines rendered with a Line MAT
  with width taper -- verify the exact op set against the wiki before
  building). Two shells: outer r=1 (freq 2-3), inner r=0.45 (freq 1-2),
  counter-rotating a few deg/min. An optional third, fainter gauze shell adds
  depth if the perf gate allows.
- **Spines**: 20-40 tapered cones/strands instanced on outer-shell vertex
  normals, lengths varied +-20%, one or two percent angular jitter.
- **Vertex life**: small emissive sprites instanced at lattice vertices;
  their intensity carries the traveling pulse (drive by distance-from-a-
  moving-origin through a CHOP or shader ramp).
- **Marine snow**: a few hundred POP particles drifting slowly (load
  `/pop-networks` first), rendered as soft sprites, placed to sit mostly
  outside the focal plane.
- **Rig**: perspective camera in portrait, slow orbit (60-90s period) with a
  few degrees of elevation drift; ONE key rim light (cyan, behind/above), a
  very low slate fill, and an amber point light inside the inner shell.
  Depth of field via the render/depth chain (depth-keyed defocus -- verify
  the current best recipe against the wiki; the organism stays tack-sharp,
  snow melts).
- **Post**: bloom (emissives only), chromatic aberration at edges, vignette,
  grain, final grade protecting the teal-black floor.

## Parameters (exactly these five)

| Param | Range | Does |
|---|---|---|
| `Spin` | 0-1 | Orbit + shell counter-rotation rate (1 is still slow) |
| `Pulse` | 0-1 | Bio-pulse frequency (0 = dormant) |
| `Spines` | 0.5-1.5 | Spine length multiplier |
| `Rimhue` | 0-1 | Rim-light hue walk around the cyan-teal band |
| `Focus` | 0-1 | DOF strength (0 = deep focus, 1 = macro-thin) |

## Notes

- This is the heaviest build of the five: gate performance after the lattice,
  after the lights/DOF chain, and after the snow. Line/strand rendering and
  the defocus chain are the likely costs -- measure, do not guess.
- Keep the scene graph disciplined: `geo_shell_outer`, `geo_shell_inner`,
  `geo_spines`, `geo_snow`, lights and cam named for their roles, annotated
  per rig section (geometry / lighting / camera / post). Related ops sit
  together -- the MAT beside its geo, the lights beside the render.
- Verify the pulse and the counter-rotation on camera with timed capture
  pairs (t / t+10s / t+20s).
- Hero frame: mid-pulse, rim light raking the lattice, two or three bokeh
  motes floating in the foreground dark.
