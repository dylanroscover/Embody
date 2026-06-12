# BUILD TASK: "Murmuration" — GPU Flocking Swarm (TouchDesigner POPs)

> Hand this whole file to a fresh agent (Claude Code) running inside the Embody TouchDesigner project with Envoy MCP tools. It is self-contained. It was hardened by a multi-agent research + review pass; the "verify live" instructions are deliberate — POPs are a new operator family and exact names/params must be confirmed in the running build, not trusted from memory.

Build a self-contained, purely-procedural, headless-capable artwork called **Murmuration**: a dense GPU particle swarm that behaves like a starling murmuration at dusk — cohering, separating, aligning, splitting into vortices and reforming around a slow invisible attractor, with curl-noise wander. It is a **hero clip for an Embody/Envoy demo reel**: the visual must be strong enough to sell the tool, and the network must be clean enough to appear on screen as proof that an AI authored a real, readable, **version-controlled** TD network.

**Particle count is an OUTPUT, not a target.** Find the largest count the host sustains at ≥30 fps with behavior intact, and report it. A smooth 50k beats a stuttering 500k. Any figures below are starting points, not quotas.

---

## Required skills (load before acting)
1. `/mcp-tools-reference` — **first MCP call of the session**; confirm tool names/signatures.
2. `/create-operator` — operator creation, parameter, connection, and verification workflow.
3. `/manage-annotations` — annotation bounds + coordinate math.
4. `/td-api-reference` — TD Python API + the POPs section.
5. `/debug-operator` — load before diagnosing any non-empty `get_op_errors` result.
6. `/externalize-operator` — only needed if you use `externalize_op`/`save_externalization`. For the `export_network`/`import_network` path used here it is optional but recommended.

**Skills are preferred, not blocking.** If a skill, OP Snippet, or example `.toe` is unavailable, continue with live MCP discovery (`query_network`, `create_op` smoke tests, `get_op`, `get_parameter`, `get_op_errors`, `execute_python`). Never stop solely because a reference is missing. Also follow `CLAUDE.md` and `.claude/rules/`, and read `demo/AGENT_BRIEF.md` (house style for Embody demo networks).

---

## Definition of Done (verify ALL — objective checks, not vibes)

1. **Renders in real time.** Report the actual live particle count (read from the final POP's point/particle count via `execute_python`, NOT from the configured max/birth params) and measured fps.
2. **Measurably distinct flocking — proven two ways, both required:**
   - **Numeric (the key anti-blob guardrail):** via `execute_python`+numpy, sample `P` and `PartVel` from the final sim POP and assert: (a) **separation** — median nearest-neighbor distance is between ~3% and ~25% of the bbox diagonal and not trending to zero; (b) **alignment** — mean neighbor velocity cosine-similarity is `> 0.25` and `< 0.95` (not random, not lockstep); (c) **cohesion** — neighbor-count variance `> 0` (local clusters, not one uniform cloud). **Then toggle each force's weight to 0 and confirm its metric collapses** — this proves the *force* is doing it, not the noise. Record all numbers.
   - **Visual:** the captured frame reads as flowing, alive — not static dots, a rigid lattice, or a single orbiting blob.
3. **Continuously evolves, never freezes.** Sample positions at t=0/2/5s; ≥95% of points must change position by > epsilon each interval. The feedback loop integrates every frame.
4. **Single renderable output TOP** (`capture_top`): dark background, luminous particles, color mapped by speed or age, optional subtle trails/bloom.
5a. **Zero errors:** `get_op_errors recurse=True` clean on the finished COMP.
5b. **Purely procedural & headless:** scan all ops — no Movie File In, Audio Device In, camera/sensor/hardware inputs, or external file params. Procedural Camera/Light COMPs allowed only on the fallback render path.
6. **Clean, readable layout** per `.claude/rules/network-layout.md` + `demo/AGENT_BRIEF.md`: 200-grid, left→right flow, no overlaps, every op inside a titled annotation, docked DATs repositioned.
7. **Captured frame** saved as `screenshots/{number}_{name}.png` (e.g. `screenshots/01_murmuration.png`), plus an honest self-critique vs the art direction.
8. **Exported to `.tdn`** and re-imported into a scratch COMP (`clear_first=True`) with verified fidelity (op count + connections + valid `targetpop` + clean errors + still-moving particles), then scratch deleted.

---

## Critical guardrails (read once, obey throughout)

- **Operator-name contamination — TD ≠ Houdini.** TouchDesigner has **NO** `POP Steer Cohesion`, `POP Steer Align`, or `POP Flock` — those are Houdini. Build flocking by hand from POP math + neighbor analysis. The combine op is **Attribute Combine POP** (`attributecombinePOP`), never `combinePOP` (doesn't exist). The stable force op is **Force Radial POP** (`forceradialPOP`); a plain `forcePOP` lives only in the Experimental namespace (a unified Radial/Axial/Spiral/Vortex node) — prefer `forceradialPOP` on stable builds.
- **Neighbor aggregation uses Neighbor POP, NOT Proximity POP.** `neighborPOP` outputs neighbor *averages* of `P`/`PartVel` — the actual flocking primitive. `proximityPOP` builds connection *lines* between nearby points (visualization only). Use Proximity only for optional line viz. **Verify live** that Neighbor POP exists and what it outputs.
- **Forces live INSIDE the feedback loop.** Anything that modifies `PartForce`/`PartVel`/`P` for the next frame must sit between `particle_pop` and the downstream feedback `null_sim`. A force op after the Null does nothing.
- **Topology-changing ops live OUTSIDE the loop.** Trail POP, Proximity line output, and any op that changes point count must be on a side branch *after* `null_sim` (toward the renderer) — never inside the `targetpop` loop, or it corrupts the sim.
- **GPU only for the sim.** Python is for setup, expressions, batch construction, and verification — never per-frame simulation. Flocking math stays on the GPU (POP math nodes or a GLSL POP).
- **No-blob rule.** Do not declare success on a noisy cloud, a static sphere shell, or particles merely orbiting one attractor without local flocking.

---

## STEP 0 — Verify environment & discover exact names (do first, don't skip)

1. `get_td_status`; if TD isn't running, `launch_td`. `get_td_info` — POPs require **2025.30000+**. If older: STOP, report POPs unavailable, offer the reaction-diffusion ("Morphogenesis") concept as fallback.
2. **Find the build location.** Try `result = ui.panes.current.owner.path`; if that fails or TD is headless, use the project root from `query_network('/')`. Build inside a clearly-named **`baseCOMP` `Murmuration`** there. **Never `/local`** (volatile). If `Murmuration` exists, use `Murmuration_1`.
3. **POP-probe pass — harvest real optypes, params, and attribute names.** In a disposable scratch COMP, `create_op` each type you intend to use, read its true optype + parameter names/menus/defaults + output attribute names (after one cook) with `get_op`/`get_parameter`/`execute_python`, then delete the scratch. Save findings to `verification/pop_probe.json` and cite it in your report. Verify at minimum:
   - **Expected present** (repo `/td-api-reference`): `baseCOMP`, `spherePOP`, `gridPOP`, `particlePOP`, `noisePOP`, `mathPOP`, `nullPOP`, `glslPOP`, `transformPOP`, `mergePOP`, `selectPOP`.
   - **Smoke-test before relying on:** `neighborPOP`, `proximityPOP`, `forceradialPOP`, `trailPOP`, `rendersimpleTOP`, and a math-combine op (`mathmixPOP`/`mathcombinePOP`).
   - **Confirmed optype** (create-test once to be safe): Attribute Combine POP = `attributecombinePOP` (never `attribcombinePOP` or `combinePOP`).
   - For coloring: in POPs the color attribute is **`Color`** (vec4 RGBA; components `Color(0..3)`) and the point-size attribute is **`PointScale`** — NOT the SOP names `Cd`/`pscale` (the SOP→POP importer renames `Cd`→`Color` and `pscale`→`PointScale`). Confirm live before writing color/size expressions.
   If a listed optype differs, use the live name and document it; if unavailable, adapt to the closest verified POP-native route.
4. **Decide the render path now, not mid-build.** Smoke-test `rendersimpleTOP`. If it exists → primary path (renders a POP directly, auto camera+light). If not → fall back immediately to Geometry COMP + Camera + Light + Render TOP and don't retry. Record which you used.
5. **Tiny feedback-loop test before anything else:** `spherePOP → particlePOP → nullPOP`, set `particlePOP.targetpop` to the null (relative ref `./null_sim`), pulse `initializepulse`, `startpulse` if the build needs it, confirm `play`. Capture two frames ≥2s apart and confirm a handful of particles MOVE. Only then add forces and scale.

---

## Architecture

GPU feedback particle system. The Particle POP births + integrates; downstream POPs compute/add forces; a downstream Null POP is the feedback target for the next frame.

```
source_points (spherePOP / gridPOP, jittered — no visible grid/shell)
  → particle_pop (particlePOP; targetpop = ./null_sim)
  → neighbor_analysis (neighborPOP — neighbor averages of P, PartVel)
  → flock_force (cohesion + separation + alignment → PartForce)   [Math POPs or one glslPOP]
  → attractor_force (forceradialPOP: slow-moving radial pull + mild spiral)
  → curl_wander (noisePOP Curl-3D NoiseCurl, added gently to PartForce)
  → null_sim (nullPOP)        ← particle_pop.targetpop points HERE (relative)
        ├─(feedback up to particle_pop)
        └→ [side branch, OUTSIDE loop] optional trailPOP / color → render → optional bloom → null_render
```

**Particle POP setup (per `demo/AGENT_BRIEF.md`, confirmed):** emitter → input 0; `targetpop` (UI label "Target Feedback Loop POP") = downstream null as a **relative** reference (`./null_sim` or `null_sim`, never absolute `/...`); keep time integration on (unless you intentionally replace it with a GLSL POP and prove it works); set birth/life/max so the steady-state population holds near target (rule of thumb: ≈ `birth_rate × life` — a heuristic, not a TD spec; always read the live count); **pulse `initializepulse` after wiring `targetpop`**, then `startpulse` if required, confirm `play`. Re-pulse after any change to source/target/lifecycle/topology. Particle attributes (exact names): `P`, `PartVel`, `PartForce`, `PartMass`, `PartDrag`, `PartAge`, `PartLifeSpan`, `PartId`, and optional `PartDeath` (kill flag).

**Force accumulation — reset each pass.** At the start of each feedback pass, zero or freshly compute `PartForce` (don't keep adding to stale fed-back force), THEN sum cohesion + separation + alignment + attractor + curl. Clamp/scale total force so particles can't explode numerically.

---

## Flocking implementation (Reynolds, on GPU)

**Preferred — Neighbor POP + math:**
1. **Neighbor POP**: distance-based; tune `maxdistance`, `maxneighbors`, `numhashbuckets` (set ≈ active particle count). Output averaged neighbor `P` and `PartVel` (weighted/closest if more organic).
2. Compute three steering vectors (Math/Attribute-Combine POPs, or one GLSL POP):
   - **cohesion** = `avgNeighborP − P`  (low weight)
   - **alignment** = `avgNeighborPartVel − PartVel`  (medium weight)
   - **separation** = inverse-distance (or inverse-square) repulsion from neighbors inside the separation radius  (highest weight — prevents collapse)
3. Sum into `PartForce` (not a disconnected custom attr). Confirm `null_sim` carries a valid `PartForce`.
4. **Force Radial POP** for large-scale motion: a slow-moving attractor via `pos` or a `specpop` Point POP; radial pull + mild spiral for vortex/split-reform. **Verify force sign visually** (don't assume +radial = pull). Chain multiple if stacking is clearer.
5. **Noise POP curl wander**: enable Curl-3D (`NoiseCurl`), scale small, add to `PartForce`; animate noise space via an `absTime.seconds` expression (NOT a per-frame Python callback). Keep subtle so flocking stays legible.

**GLSL-POP fallback (use without hesitation):** if Neighbor POP + math POPs can't express the neighbor reduction cleanly within ~30 min of trying, implement the whole flock update (neighbor reduction + 3 forces + integrate) in a single `glslPOP` inside the feedback loop. It's part of the POP family and stays on the GPU — a working GLSL flock beats a broken node-graph approximation. Preserve the same visual goals and the same numeric verification. If you move integration into the GLSL POP, set the Particle POP's `timeintegration` OFF — this **disables the built-in drag and damping**, so the shader must reimplement velocity/position integration (and any drag/damping) itself.

---

## Starting tuning values (start here, then tune by looking at captures)

Provenance: the only **published TD-validated** flocking weights (Derivative forum "Flocking/Boids GPU") give the *shape* — separation ≫ attractor > alignment > cohesion. Magnitudes are scaled to your force output, so treat numbers as seeds.

- Test 4096 → tune behavior → scale (see Performance ladder). Prefer a beautiful 75k–150k stable over a stuttering bigger number.
- `maxparticles`: above steady-state (start ~20k pool). Birth/life so population holds (e.g. life ~8–12s).
- `maxneighbors`: 8–24. Neighbor radius ~0.1–0.25 after normalizing source to ~unit scale. Separation radius ~30–45% of neighbor radius.
- Weights (normalized): **separation 1.0**, **attractor 0.8–0.9**, **alignment 0.3**, **cohesion 0.1**, **curl low**, **spiral weak** (shapes, doesn't dominate).
- Velocity damping ~0.97–0.98 (prevents runaway; too low = collapse/explosion). Mass ~1.0. Drag and damping are separate — tune one at a time.
- **Tuning order:** alive & persistent → separation alone (spreads evenly) → cohesion (loose clumps) → alignment (starts flowing) → moving attractor (swirls) → curl last (organic break-up).
- **Tuning budget:** ~5–8 capture→adjust cycles, hard stop ~12. If the numeric flocking metrics pass but the look is still weak at budget, ship the best version and document what you'd change.

---

## Performance (be realistic — host may be a fanless M2 MacBook Air)

**The neighbor search is the real cost, not the point count** — flocking is O(points × neighbors). Realistic targets: **~30k–150k on modest Apple Silicon (M2 Air), ~500k stretch-only; 500k+ for M3/M4 Max-class.** Do not chase "millions" on a laptop.

**Adaptive ladder:** start 4096; after behavior is correct, ×1.6 per step (4k, 6.5k, 10k, 16k, 26k, 42k, 67k, 107k, 172k, …). Run each step ~15s; require **≥45 fps while tuning, ≥30 fps final floor, p10 ≥24, dropped frames <2%**, no flock collapse. Stop at the first step that fails; back off to the previous stable count (or 0.8× the failing count).

**Levers (highest impact first):**
1. **Disable node previews/viewers** during sim — confirmed ~2× fps swings on Mac POP nets.
2. **Never feed a per-frame-changing value into a shader-embedded constant** (Math Combine/Mix "Scope B") — it recompiles the shader every frame and tanks fps. Route changing values through the **Uniforms** tab / a referenced uniform instead.
3. **Neighbor POP `numhashbuckets` ≈ particle count**; cap `maxneighbors` (drop to 8–12, then 4–8 at high counts).
4. `freeextragpumem` on heavy POPs; minimize per-point attributes (each is allocated per point — unified memory is the binding constraint on Apple Silicon).
5. At 100k+: simplify force math, speed-color only, drop expensive trails/bloom unless fps headroom remains.

macOS notes: Apple Silicon is the supported Mac path (Intel+AMD POPs were broken until late-2025 builds). MoltenVK gaps: no atomic-float / double-precision / hardware ray tracing. 16GB+ unified memory recommended; Mac POP fps is build-sensitive.

---

## Rendering & art direction (this is a HERO clip — push for festival-grade)

Target: a luminous starling murmuration at dusk — one living organism of light, slow majestic flow punctuated by compression/expansion bursts.

- **Background:** near-black (slight dusk navy ok).
- **Color (`Color`):** 3-stop velocity (or age) ramp — slow = deep blue-violet, mid = ice-cyan/white, fast tips = warm amber/gold. No rainbow palettes, no flat single-color dots. Build via Math POP (`length(PartVel)` → normalize) → ramp lookup (Lookup-Texture POP + Ramp TOP) → `Color`, or in the GLSL POP. (The POP color attribute is `Color`, not the SOP name `Cd`.)
- **Glow:** additive blending + Bloom TOP tuned so dense cores glow as luminous clouds with soft halos while individual points stay visible at edges — not clipped white blobs.
- **Depth/volume:** perspective-correct point sprites (nearer = larger/brighter), subtle fog so the swarm reads as a 3D volume, not a 2D sheet. For 100k+, render as point **sprites** (Point Sprite MAT), not instanced meshes.
- **Trails (optional, side branch):** Trail POP matched by `PartId` (Attrib-is-UInt on), ~6–18 frame persistence, additive, fading to transparent via the trail Age attribute. Reveal flow on turns without smearing the frame.
- **Composition:** frame the swarm as a diagonal S-curve / vortex ribbon filling ~70–85% of width with negative space on one side; avoid a centered circular blob.
- **Camera:** slow 8–12s push/arc through the swarm edge with visible parallax — majestic, observant, not static or handheld.
- **Motion pacing:** the continuous system should naturally show phases — loose flowing ribbons → compression into a dense knot with rotational shear → split and rejoin. Animate attractor/noise slowly so it isn't constant-speed drift.
- **Density contrast:** tight luminous knots + feathered outer wisps + occasional gaps. Reject any frame that reads as flat, uniform dust.

Render paths: **primary** = Render Simple TOP (`pop` = `null_sim`, dark `bgcolor`, normalize/camera as needed) → optional Bloom/Level/Composite → `null_render`. **Fallback** = Geometry COMP + Camera + Light + Render TOP (keep procedural, headless, annotated).

---

## Embody workflow (build live, then version-control)

1. **Build + tune LIVE via MCP** — `create_op` per op; `batch_operations` for bulk param/wire/position (CLAUDE.md rule #12, never 3+ individual same-tool calls); `execute_python` for computed setup/expressions/verification; `capture_top` to iterate. After each creation batch, `get_op_errors recurse=True` and fix immediately.
2. **Once the look is locked, `export_network`** the `Murmuration` COMP to a `.tdn` (with `output_file`). **Verify the file exists** and shows up via `get_externalizations`/`get_externalization_status` — only then is it a real version-controlled artifact.
3. **Smoke-test round-trip:** `import_network` with `clear_first=True` into a scratch sibling COMP (e.g. `Murmuration_verify`). Confirm same op count + connections, a **valid relative `targetpop`** (`./null_sim`, not absolute), clean errors, and still-moving particles. TDN supports POP operators, wired POP chains, generic built-in params, and POP-reference param styles — but the specific `particlePOP.targetpop` round-trip isn't separately test-covered in this repo, so **prove it in this build** rather than assuming. Then `delete_op` the scratch.
4. Never edit `externalizations.tsv` directly.

---

## Verification loop (numeric — don't rationalize success; iterate on any failure)

Save machine-checkable artifacts under `verification/`. A failed threshold requires tuning and rerunning the loop — do **not** waive failures with visual judgment.

1. **Errors:** `get_op_errors recurse=True` clean.
2. **Sim telemetry** (`verification/motion_metrics.json`): sample `null_sim` `P`/`PartVel` at t, t+60, t+120, t+180 frames — point count, bbox, centroid, median + p95 speed, median displacement from frame 0. **Fail** if median displacement over 120 frames < 2% of bbox diagonal, median speed < 0.01, p95 > 25× median, bbox diagonal changes > 4×, or final bbox < 15% of initial.
3. **Flocking proof** (Definition of Done #2): nearest-neighbor + alignment + cohesion metrics on a ≥2000-particle sample, **plus the toggle-each-force-to-zero control**.
4. **Render sanity** (`verification/` image stats on 3 frames 2s apart): **fail** if mean luminance < 0.01, non-black pixels < 1%, saturated pixels > 20%, or frame-to-frame pixel diff < 0.5% (static).
5. **Performance** (`verification/perf.json`, measured 10s after warmup): avg fps ≥30, p10 ≥24, dropped <2%, live count within ±5% of reported.
6. **Layout:** `get_network_layout` — no overlaps, annotations enclose groups, left→right, docked DATs repositioned.
7. **Re-import check** (`verification/reimport_check.json`): per Embody-workflow step 3.

**Definition of done = all numeric sim/image/perf thresholds pass + zero errors + final frame passes critique + `.tdn` re-imports + the same telemetry passes after re-import.**

---

## Common failure modes
- **Frozen:** `targetpop` missing/wrong/absolute; `initializepulse` not fired; play off; forces too weak vs mass/damping.
- **Forces ignored:** force/math/GLSL op placed after the feedback Null or on a non-feeding branch.
- **Collapse:** cohesion/attractor too strong, separation too weak, damping too low.
- **Explosion:** separation/curl too strong, force unclamped, mass too low.
- **No real flocking:** attractor/noise dominates local neighbor rules (the classic "looks like flocking but it's just drift" cheat).
- **Perf cliff:** `numhashbuckets` ≪ count; `maxneighbors` too high; node previews on; per-frame shader-constant recompiles; heavy trails/bloom before behavior is tuned.
- **Black render:** `pop` not set; bad alpha/bg; wrong camera distance/normalize; final TOP not cooking.
- **Fake hero frame:** one lucky still, no continuous motion — captures seconds apart must differ.
- **Wrong family:** Houdini names, `combinePOP`, or Experimental `forcePOP` used without live proof.

## Final deliverable report (concrete facts only)
Final `Murmuration` COMP path; final render TOP path; `screenshots/` path; exported `.tdn` path; re-import verification result; full operator list with paths; tuned values (count, birth, life, maxparticles, neighbor radius, maxneighbors, hash buckets, the flocking weights, damping/drag, attractor/spiral, curl); measured fps + dropped-frame notes; the `verification/*.json` artifacts. One honest self-critique: what works, what still falls short, what was compromised for performance or operator limits. **"Done" is wrong if any verification step was skipped — say so plainly if it was.**
