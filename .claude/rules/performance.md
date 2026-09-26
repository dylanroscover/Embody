# TouchDesigner Performance and Crash Avoidance

Watch the metrics while building and never freeze or crash the user's session. Target TouchDesigner 2025+. The crash-cause table and the full safe-default caps live in `/td-api-reference` and its `references/heavy-build-safety.md`; load them before any heavy build (render chains, feedback loops, instancing, large geometry, GLSL, high-resolution TOPs, many operators at once). A show is soaked, not spot-checked: `/testing` and `run_soak_test`.

## The gating protocol

1. **Before building**: `get_project_performance(include_hotspots=5)` and record the baseline: `timing.fps`, `frameTimeMs`, `cookRate`, `cookRealTime`, `timeSliceMs`; `memory.gpuMemUsedMB`, `totalGpuMemMB` (headroom = total minus used), `cpuMemUsedMB`; `frameHealth.droppedFrames`, `activeOps`, `totalOps`; `hotspots[]` (path, cpu/gpu/combined cook ms, memory bytes). GPU temperatures are advisory; `-1.0` means unknown.
2. **After each significant step**: re-run it and compare. Do not build everything and check once at the end.
3. **Localize regressions**: `get_op_performance(op_path, include_children=True)` on suspects; compare cook times, memory and cook counts.
4. Layout discipline still applies (`network-layout.md`).

## Units

Every TD time metric is milliseconds: `frameTimeMs`, `timeSliceMs`, hotspot cook times, and every OP cook-time member. The frame budget is 16.7 ms at 60 fps, 33.3 ms at 30 fps; state the unit in every number you report. Never microbenchmark a Python snippet against frame time: per-frame cost is how often the code runs per frame times cook scheduling and GPU sync, so measure at the cook level (`get_op_performance` deltas idle versus active, `get_project_performance` across the change).

## Stop conditions (halt, report the metric and the op path, propose a bounded alternative)

| Signal | Stop threshold |
|---|---|
| FPS below target | `timing.fps` under ~90% of target (`< 54` at 60 fps, `< 27` at 30) |
| Frame time over budget | `frameTimeMs > 16` at 60 fps, `> 33` at 30 |
| Dropped frames | `droppedFrames` increases between checks on a real-time project |
| GPU memory danger | headroom under 20% of `totalGpuMemMB`: stop allocating TOPs and instances |
| CPU memory climb | `cpuMemUsedMB` climbs monotonically across checks with no new ops (leak or unbounded buffer) |
| GLSL failure | a GLSL op's info DAT shows compile errors, or any "Vulkan Device has returned a Fatal Error" |
| Feedback runaway | a feedback loop's `gpuCookTime` or `gpuMemUsedMB` rises every frame with no input change |
| Cook cascade | a Null/In/Out op with a large `cpuCookTime`, or `totalCooks` climbing every frame while idle |
| Main-thread pin | one `execute_python` or build call spikes frame time, freezes the UI, or nears the 30 s MCP timeout: chunk the work |

## Safe caps (summary; detail and rationale in the reference)

- TOPs at most 1920x1080 unless asked; 8/16-bit fixed formats over 32-bit float; check `w*h*channels*bytes` against GPU headroom first.
- Feedback loops always bounded: fixed resolution inside, decay below 1, Reset wired, bypassed while wiring; every chain terminated in a Null.
- Bypass while wiring: never leave Movie File In, Audio, Render, Timer, feedback, output or viewer-driven ops cooking while building around them.
- GPU instancing or POPs over Copy SOP; start modest and ramp with metrics (CPU particles ~10k max); transform at the Geometry COMP level.
- CHOPs: Time Slicing on, small sample rates and Trail/buffer windows; Audio File In, not Audio Play, for long files.
- GLSL: constant-bounded loops only; guard dynamic indexes with `TD_NUM_*`; read the info DAT before relying on the op.
- `execute_python`: short, non-blocking; no sleep or synchronous I/O on the main thread; no `TOP.sample()` in loops; chunk large builds across frames.

Movie or image-sequence export: load `/movie-export` first (the default Realtime flag silently produces duplicate-frame judder). Diagnosing CPU versus GPU: if dropping the render resolution to 64x64 does not raise fps, the bottleneck is CPU. After a crash TD writes `CrashAutoSave.<project>.toe`, which opens bypassed.
