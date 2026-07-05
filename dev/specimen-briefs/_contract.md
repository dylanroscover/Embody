# Build Contract -- binding for every brief in this folder

You are executing ONE commission brief from this folder. The brief is the art
direction; this contract is the engineering discipline. Both are binding. Where
they genuinely conflict, keep the brief's LOOK contract, adapt the technique,
and say so at the review gate -- never silently reconcile.

This is written for a **clean Embody + Envoy install** (a workshop machine). It
assumes ONLY what ships with Embody: the Envoy MCP tools, the shipped `.claude/`
rules, and the shipped skills. It does NOT assume any gallery, manifest, or web
project. The deliverable is a beautiful, self-contained, animated COMP living in
YOUR TouchDesigner project.

## Session start (in this order)

1. **Confirm TD is reachable.** Find `get_td_status` (it is served from cache
   even if TD is down); call it. If TD is not running, `launch_td` and wait for
   `connected:true`. If it does not connect, wait ~10-15s (it self-heals) before
   doing anything drastic; load `/td-recovery` only if it is still down after that.
2. **Load skills BEFORE acting** (they are prerequisites, not optional reading):
   - `/mcp-tools-reference` -- before your first MCP tool call
   - `/visual-aesthetics` -- these are art pieces; it applies in full, all the way through
   - `/create-operator` -- before `create_op`
   - `/parameter-design` -- before designing the custom parameters
   - `/td-api-reference` -- before any `execute_python`, and for the cook model / threading
   - `/manage-annotations` -- before `create_annotation` / `set_annotation`
   - `/pop-networks` -- before ANY POP / particle / GPU-point work (briefs that need it say so)
   - `/externalize-operator` -- before the optional externalize-to-TDN finish
   - `/movie-export` -- ONLY if you record a clip (optional)
3. If, and only if, you are sharing one TD instance with another session, a
   `_peers` advisory will ride back on tool responses -- load
   `/multi-session-etiquette` then. On a solo workshop machine, ignore this.

## Where to build (do NOT assume the network layout)

- `query_network` on `/` to discover the ACTUAL project root -- never assume
  `/project1`.
- Create ONE container COMP at the project root, named exactly as the brief's
  "Build as" line (e.g. `/point_line_plane`). Build the entire piece inside it.
- **NEVER build under `/local`** (volatile, not saved with the `.toe`).
- The piece must be self-contained inside that one COMP: its own sources,
  its own parameters, terminating in an Out TOP named `out1`.

## The cook-demand trap (read this -- it bites every time)

A chain that references time only cooks when something DEMANDS its output (see
the Cook Model in `/td-api-reference`). In a build sandbox nothing is watching,
so a correct animated network sits frozen on frame 0 and a single capture proves
nothing. Defend against it:

- **Output-first.** Create `out1` and turn its display flag ON before you build
  the chain; keep the working chain wired into it so the network-editor viewer
  demands cooks.
- **Add a temporary frame-driver** at the PROJECT ROOT (outside your COMP, so it
  never becomes part of the piece): an Execute DAT whose `onFrameStart` runs
  `op('<your_comp>/out1').cook(force=True)`. This guarantees real per-frame
  cooking while you iterate. Delete it before you finish.
- **Prove animation with TWO captures across real time**, never one: capture,
  let real wall-clock seconds pass (a background `sleep`, or scheduled frames
  via `run(..., delayFrames=N)`), capture again, compare. If the content
  genuinely changed (beyond a pure rotation), the motion is real.
- Feedback and particle sims accrue state over REAL frames -- a synchronous
  `for` loop of `cook(force=True)` does not advance them. Drive them with the
  frame-driver and let wall-clock pass, then capture.

## Performance gating (hard stops, not advice)

Follow your shipped **performance** rule. In short: baseline
`get_project_performance(include_hotspots=5)` BEFORE building, re-check after
each heavy step (feedback loops, GLSL, high-res TOPs, instancing, POPs), and
treat its stop conditions as hard stops -- fps below ~90% of target, GPU
headroom under 20%, climbing dropped frames, GLSL compile errors in the info
DAT. Respect each brief's resolution budget; never freeze or crash the machine.
Take fps readings WITHOUT a concurrent `capture_top`/`numpyArray()` -- those
stall the GPU and make the reading dip.

Two techniques you will reuse:
- **Static source + cheap motion**: a heavy generator (high-octave noise, a big
  sim) cannot re-render every frame at high res. Make it STATIC (remove every
  time reference so it cooks once and caches) and animate a cheap downstream op
  -- drift/rotate/warp the SAMPLE coordinates, not the source. Confirm with
  `cookedThisFrame`: source `False`, animated op `True`. (Genuine feedback sims
  are the exception -- keep them bounded and low-res.)
- **Bound every feedback loop**: fixed resolution inside the loop, a decay
  multiplier < 1, a Reset wired, bypassed while you wire it.

## Layout discipline

Follow your shipped **network-layout** rule in full: annotate per logical
section, extend groups rightward / new chains downward, compute every offset
from real `nodeWidth`/`nodeHeight` (never a fixed step), and run the Verify gate
(`get_network_layout`) after every batch -- nothing at (0,0), no overlaps, wires
flow forward, every docked DAT hugged to its host. GLSL ops are the classic
trap: a GLSL TOP/MAT/POP docks pixel/compute/info DATs that scatter onto
neighbors -- hug them in the SAME `execute_python` that creates the op, never
defer. `create_op` auto-positions; `execute_python`/`.create()` does NOT, so you
own the placement there. Name processing ops `optype_name` (`glsl_warp`,
`instance_lines`); the terminal is always the Out TOP `out1`.

## ASCII punctuation in every file you generate

Everything you write to disk (this includes any `.py`, `.md`, `.glsl`, `.json`)
must be plain UTF-8 with ASCII punctuation only: `--` not an em dash, `->` not an
arrow, `...` not an ellipsis, straight quotes, `x` not a multiply sign. No BOM.
This keeps the exported network and its shaders clean everywhere.

## The look loop (this IS the work)

- Iterate: `capture_top` on `out1` -> READ the frame -> grade it against the
  brief's **Look Targets** -> refine. At least five serious iterations before
  you consider it close. The first pleasing frame is never the final one.
- `capture_top` force-cooks, so it can show a frame your idle viewer is not --
  a stale viewer vs a fresh capture is a real difference, not a glitch.
- Judge MOTION, not a lucky still: capture at t, wait 10-20 real seconds,
  capture at t+, and confirm both frames satisfy the Look Targets AND that the
  pair shows the motion the brief demands.
- Score every Look Target 0-10 honestly. Target is 8+ on each. A single
  violated Anti-Goal means NOT DONE regardless of scores.
- Success = captured frames a stranger would call stunning, a clean readable
  network, and parameters that visibly work. Not "the chain is built."

## Parameters

- Design them via `/parameter-design`, on a proper custom page on your COMP.
- Exactly the parameters the brief names -- same names, same ranges.
- Prefer single-component params (Float/Int/Menu/Toggle). If you externalize to
  TDN later, note the round-trip gotcha: a MULTI-component par (RGB/XYZ) whose
  value equals its default can re-import as zero -- so prefer a single `Hue`
  float over an RGB group, or give such a par a value that differs from its
  default. Single-component params are always safe.
- Every param must visibly do something. Test BOTH ends of every range on
  camera (capture) before calling it exposed.

## Review gate -- STOP here

Before you finish, STOP and present to the artist (the workshop participant):

- 2-3 captures at different time offsets (proving the motion),
- a one-line self-grade for each Look Target,
- any place you adapted the technique spine, and why.

Do not proceed to the finish step until the artist is happy. If a Look Target
is below 8, say so plainly and keep iterating rather than shipping it.

## Finish (once approved)

1. **Tidy the network**: delete the temporary frame-driver (it lives outside the
   COMP). Run the layout Verify gate one final time. Turn `out1`'s display flag
   on so the piece animates whenever it is viewed or rendered.
2. **Save the project** (`project.save()` with no arguments) so the work
   persists in the `.toe`.
3. **Optional but encouraged -- externalize to a git-friendly TDN file.** This
   is the whole point of Embody: load `/externalize-operator`, externalize your
   COMP with the TDN strategy so it is written to disk as a diffable `.tdn`
   (include DAT content so the shaders/scripts are captured). Then verify the
   round-trip cheaply: re-import the exported `.tdn` into a throwaway COMP via
   `import_network`, confirm a couple of param `.val`s and that `out1` renders,
   and destroy the temp COMP.
4. **Capture a hero frame** for the artist to keep -- drive the piece for the
   brief's "settle before capture" frame count first (feedback/particle pieces
   need real frames to develop), then capture `out1`, read it, and judge it
   against the brief. An undeveloped feedback sim captures as a near-black frame
   -- that is the cook-demand trap, not a broken build.

One piece per session. Do not start a second brief in the same session.
