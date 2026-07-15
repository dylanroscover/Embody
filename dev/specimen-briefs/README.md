# Commission Briefs -- art-directed TouchDesigner works

Ten director's briefs for building exceptional animated works in TouchDesigner
with Envoy. Each is a self-sufficient prompt a fresh Claude Code session can
execute end-to-end: build the piece live in TD, iterate against the Look
Targets, stop for the artist's review, then save (and optionally externalize).

`_contract.md` is the shared engineering discipline -- **every brief requires
it.** The brief is the art; the contract is the craft.

## Environment -- a clean Embody install (workshop-ready)

These are written to run on a fresh Embody + Envoy install with nothing
special added. They assume ONLY:

- the Envoy MCP tools,
- the shipped `.claude/` rules (network-layout, td-python, performance,
  parameters, mcp-safety, td-connectivity, multi-session), and
- the shipped skills (`/visual-aesthetics`, `/create-operator`,
  `/parameter-design`, `/td-api-reference`, `/manage-annotations`,
  `/pop-networks`, `/externalize-operator`, `/mcp-tools-reference`, and the rest).

No gallery, manifest, or web project is involved. The deliverable is a
self-contained animated COMP in the student's own project, terminating in an
Out TOP named `out1`, with a small set of working parameters -- optionally
externalized to a git-friendly `.tdn` at the end (Embody's whole point).

## The commissions

| # | Brief | After | Aspect | The one-liner |
|---|---|---|---|---|
| 01 | `01-point-line-plane.md` | Kandinsky (Bauhaus period) | 4:3 | A geometric composition on ivory paper that plays itself like a score -- CHOP voices striking circles, lines, and arcs |
| 02 | `02-overture.md` | Saul Bass | 16:9 | An endless generative title sequence: cut-paper bars, hard stops, swing timing, a Vertigo spiral interlude |
| 03 | `03-digital-harmony.md` | John Whitney | 1:1 | Thousands of incandescent dots under one harmonic law, crystallizing into countable rose mandalas |
| 04 | `04-lumia.md` | Thomas Wilfred | 9:16 | Folded veils of spectral light unfurling glacially in a vertical void -- the anti-plasma |
| 05 | `05-radiolaria.md` | Ernst Haeckel | 3:4 | A bioluminescent lattice organism in deep water -- full render-pipeline cinematography with DOF and a traveling pulse |
| 06 | `06-current.md` | Bridget Riley | 4:5 | A living op-art Current: a full-field line fabric vibrating at perceptual threshold -- the eye does the moving |
| 07 | `07-scan-processor.md` | The Vasulkas / Rutt-Etra | 2:1 | The raster becomes a mountain range: scanline terrain with phosphor persistence and a rolling signal surge |
| 08 | `08-desordres.md` | Vera Molnar | 1:1 | A plotter drawing that redraws itself: seeded nested squares, a migrating field of disorder, ink on warm paper |
| 09 | `09-mobile.md` | Alexander Calder | 3:2 | A mobile in a raking sun: asymmetric balance, gusts traveling down the tiers, hard shadows as a second composition |
| 10 | `10-datamatics.md` | Ryoji Ikeda | 21:9 | A monochrome data wall: figure streams, hairline rules, one slow sine giant -- frame-exact, clinically sublime |

## Why these ten (what they teach)

Each piece leans on a different corner of the platform, so the set as a whole
shows that Envoy directs far more than shaders:

- **01** -- CHOPs as choreography driving instancing (the score becomes geometry)
- **02** -- CHOP step-sequencing as an EDIT driving TOP compositing and hard cuts
- **03** -- GLSL POP parametric motion + additive sprites + feedback trails
- **04** -- bounded feedback advection, flow fields, finishing in the dark
- **05** -- the classic 3D pipeline: SOPs/POPs, MATs, lighting rig, camera, DOF
- **06** -- one meticulous analytic GLSL TOP, calibrated by eye (AA as craft)
- **07** -- a signal crossing families: TOP -> geometry (Rutt/Etra displacement),
  line rendering + phosphor persistence
- **08** -- Envoy as algorist: a seeded Python builder writing Table DATs that
  instancing turns into ink
- **09** -- hierarchical parent transforms + CHOP physics-feel + a
  shadow-casting light rig (an articulated object, no physics engine)
- **10** -- DATs as visual material on a frame-exact clock (quantization, where
  02 taught swing)

Nine aspect ratios across the ten (only the square repeats -- 03's mandala and
08's plotter grid, for opposite reasons) and ten distinct moods.

## Running a commission

One piece per session. Give the session both files -- the contract and the one
brief -- then let it run to the review gate. A kickoff prompt (adjust the path
to wherever you handed out the files):

> Read `_contract.md` and `02-overture.md` in this folder, then execute the
> commission fully. You are building ONE piece in my TouchDesigner project.
> Stop and present captures for my review before you finish.

- Each brief ends at a REVIEW GATE: the session presents captures + Look-Target
  self-grades and waits for the artist's approval before the finish step.
- For a workshop, hand each participant the contract + one brief. Everyone works
  in their own TD instance, so no cross-session coordination is needed.
- Difficulty spread: 01, 02, 06, 08, and 10 are the gentler builds (great
  openers); 03, 04, 05, 07, and 09 are advanced (GLSL POP, feedback, a full
  render rig, cross-family line rendering, and an articulated 3D rig
  respectively).

## Grading, for facilitators

The Look Targets and Anti-Goals in each brief are the rubric. A piece is done
when every Look Target self-grades 8+/10 on captured frames AND no Anti-Goal is
violated -- judged by looking at the render, not by "the network is built."
