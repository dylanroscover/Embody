# TouchDesigner Performance and Crash Avoidance

AI agents must watch performance metrics while building networks and must never freeze or crash the user's TouchDesigner session.

Target: TouchDesigner 2025+ only.

## The Gating Protocol (do this around every heavy build)

A heavy build means new render chains, feedback loops, instancing, large geometry/SOP/POP, many ops at once, GLSL, high-res TOPs, or anything cook-heavy.

1. **Before building**: call `get_project_performance(include_hotspots=5)` and record this baseline:

| Area | Exact keys to record |
|---|---|
| Timing | `timing.fps`, `timing.frameTimeMs`, `timing.cookRate`, `timing.cookRealTime`, `timing.timeSliceMs`, `timing.timeSliceStep` |
| Memory | `memory.gpuMemUsedMB`, `memory.totalGpuMemMB`, GPU headroom = `memory.totalGpuMemMB - memory.gpuMemUsedMB`, `memory.cpuMemUsedMB` |
| Frame health | `frameHealth.droppedFrames`, `frameHealth.cookedLastFrame`, `frameHealth.activeOps`, `frameHealth.totalOps` |
| GPU | `gpu.chipTemperatureC`, `gpu.boardTemperatureC` only as advisory; `-1.0` means unknown and must be ignored |
| Hotspots | `hotspots[].path`, `hotspots[].cpuCookTimeMs`, `hotspots[].gpuCookTimeMs`, `hotspots[].combinedCookTimeMs`, `hotspots[].cpuMemoryBytes`, `hotspots[].gpuMemoryBytes` |

2. **After each significant step**: re-run `get_project_performance(include_hotspots=5)` and compare against the baseline. Do not build the whole thing and check once at the end.
3. **Localize regressions**: use `get_op_performance(op_path, include_children=True)` on suspicious ops and compare `cpuCookTime`, `gpuCookTime`, `cpuMemory`, `gpuMemory`, `childrenCPUCookTime`, `childrenGPUCookTime`, `childrenCPUMemory`, `childrenGPUMemory`, `totalCooks`, and `cookedThisFrame`.
4. **Keep layout discipline**: performance work still follows [network-layout.md](network-layout.md). Do not trade crashes for unreadable or overlapping networks.

## Stop Conditions (halt and report, do not keep building)

On any stop condition, immediately STOP, report the offending metric and op path, and propose a bounded alternative before resuming.

| Signal | Stop threshold | Report |
|---|---|---|
| FPS below target | `timing.fps` below ~90% of target, such as `< 54` on 60 fps or `< 27` on 30 fps | Baseline fps, current fps, target fps |
| Frame time over budget | `timing.frameTimeMs > 16` at 60 fps or `> 33` at 30 fps | Current frame time and hotspot path |
| Dropped frames | `frameHealth.droppedFrames` increases between checks on a real-time project | Before/after dropped frame counts |
| GPU memory danger | GPU headroom < 20% of `memory.totalGpuMemMB` | Stop allocating TOPs/instances; report used, total, headroom |
| CPU memory climb | `memory.cpuMemUsedMB` climbs monotonically across checks with no new ops | Suspected leak or unbounded buffer |
| GLSL failure | GLSL TOP/MAT Info DAT has compile errors, or any "Vulkan Device has returned a Fatal Error" | Shader op path and Info DAT error |
| Feedback runaway | Feedback `gpuCookTime` or `memory.gpuMemUsedMB` rises every frame with no input change | Feedback loop path and metric trend |
| Cook cascade | Null/In/Out op has large `cpuCookTime`, or `totalCooks` climbs every frame when idle | Offending op path and cook count |
| Main-thread pin | One `execute_python` or build call spikes frame time, freezes UI, or nears the 30s MCP timeout | Chunk the work and report the blocked call |

## Crash and Freeze Causes

| Cause | Mechanism | Warning metric (threshold) | Mitigation |
|---|---|---|---|
| Resolution explosion ([Resolution TOP](https://docs.derivative.ca/Resolution_TOP), [Optimize](https://docs.derivative.ca/Optimize)) | Pixel count and TOP memory scale with width*height | `gpuCookTimeMs` spikes or GPU headroom < 20% | Clamp to <= 1920x1080, lower format, use Limit Resolution |
| Unbounded feedback loop ([Feedback TOP](https://docs.derivative.ca/Feedback_TOP)) | Loop keeps accumulating data every frame | Feedback `gpuCookTime` or `memory.gpuMemUsedMB` rises each check | Fixed resolution, decay < 1, Reset wired, bypass while wiring |
| Always-cooking operators compounding ([Cook](https://docs.derivative.ca/Cook), [Optimize](https://docs.derivative.ca/Optimize)) | Render, output, viewer, time-dependent, or export chains demand cooks every frame | `totalCooks` climbs and `cookedThisFrame` stays true while idle | Bypass during build, terminate in Null, disable viewers/outputs until measured |
| Expression-driven cook cascade ([Cook](https://docs.derivative.ca/Cook)) | Parameter references pull upstream nodes repeatedly | Null/In/Out `cpuCookTime` is large | Cache stable values, remove cross-network expressions, inspect dependent path |
| GLSL infinite loop or GPU timeout ([GLSL crash debugging](https://docs.derivative.ca/Debugging_crashes_triggered_by_GLSL_errors)) | GPU work never completes or OS resets the device | Frame time spike, UI hang, fatal Vulkan error | Constant-bounded loops only; reduce shader complexity |
| GLSL out-of-bounds array access ([GLSL crash debugging](https://docs.derivative.ca/Debugging_crashes_triggered_by_GLSL_errors)) | Illegal sampler or uniform array index can crash TD | Info DAT error or crash on cook | Guard dynamic indexes with `TD_NUM_*_INPUTS`; validate uniform array sizes |
| Huge SOP geometry on CPU ([Optimize](https://docs.derivative.ca/Optimize)) | CPU transforms or rebuilds many points/primitives | `cpuCookTimeMs` or `childrenCPUCookTime` jumps | Reduce points, keep topology stable, transform at Geometry COMP object level |
| Instance/particle count explosion ([Optimize](https://docs.derivative.ca/Optimize)) | Vertex count and buffers exceed CPU/GPU budget | `gpuCookTimeMs`, `gpuMemoryBytes`, or GPU headroom worsens | Start small, ramp with metrics, prefer GPU instancing/POPs over Copy SOP |
| CHOP sample-count explosion ([Time Slicing](https://docs.derivative.ca/Time_Slicing), [Optimize](https://docs.derivative.ca/Optimize)) | Long buffers and audio-rate samples force large cooks | `timing.cookRealTime` false, `timing.timeSliceMs` large, CPU memory climb | Enable Time Slicing, trim windows, reduce sample rate/buffer length |
| GPU memory exhaustion ([Optimize](https://docs.derivative.ca/Optimize)) | TOPs, buffers, instances, and 32-bit float textures fill VRAM | GPU headroom < 20% | Stop allocation, reduce resolution/format/count, unload unused media |
| CPU memory exhaustion | Unbounded DAT/CHOP/SOP/Python data grows until process crash | `memory.cpuMemUsedMB` climbs with no new ops | Bound buffers, clear caches, avoid accumulating Python lists/storage |
| Main-thread blocking Python ([Python threading](https://docs.derivative.ca/Python_threading_in_TouchDesigner)) | TD UI, timeline, and frame generation share the main thread | Frame time spike, UI unresponsive, MCP near 30s timeout | Chunk work, no blocking I/O or sleep, move long work off main thread safely |

## Safe-Default Caps (apply when creating risky operators)

- **TOP resolution**: default new TOPs to bounded resolution (`<= 1920x1080`). Never create 4K, 8K, or 16k unless the user explicitly asked. Before allocating, confirm `w*h*channels*bytes` against `memory.totalGpuMemMB`. Prefer 8/16-bit fixed pixel formats over 32-bit float unless precision is required.
- **Feedback loops**: ALWAYS bound them. Fix the resolution inside the loop, add a decay/multiply `< 1`, wire a Reset, and keep the loop bypassed while wiring so it is not live during construction. Terminate the loop, and every TOP/CHOP chain, in a Null.
- **Bypass while wiring**: bypass or disable cooking while wiring heavy chains. Do not leave Movie File In, Audio, Render, Timer, feedback, output, or viewer-driven ops live and cooking while building around them. Enable only after the chain is complete and measured.
- **Geometry and duplication**: cap SOP point/primitive counts. For many duplicates, use GPU instancing or POPs, not Copy SOP or `comp.copy()`. Transform at the Geometry COMP object level, not the SOP level. Keep placement readable per [network-layout.md](network-layout.md).
- **Instances and particles**: start modest and ramp up while watching `memory.gpuMemUsedMB`. Never default to millions. CPU particle systems should start around 10k max; beyond that, go GPU/instancing.
- **CHOPs**: keep sample rates and Trail/buffer windows small. Enable Time Slicing. Never create audio-scale sample-rate CHOPs without it. Use Audio File In CHOP, not Audio Play CHOP, for long files.
- **GLSL**: never write unbounded `for` or `while` loops. Cap iterations with a constant. Bounds-check every dynamic array index with `TD_NUM_*_INPUTS` guards. Check the Info DAT for compile errors before relying on the op.
- **Python via `execute_python`**: keep calls short and non-blocking. No synchronous blocking I/O or `sleep` on the main thread. No `TOP.sample()` in loops; use `numpyArray()`. Avoid `store()` in hot paths. Chunk large builds across frames. See [td-python.md](td-python.md#threading) and [td-python.md](td-python.md#cook-model).

## Movie Export / Offline Rendering (zero dropped frames)

Recording a movie with a [Movie File Out TOP](https://docs.derivative.ca/Movie_File_Out_TOP) is a heavy build. Monitor for drops DURING the render (not only after), and treat ANY dropped frame as a failed render. "Done" requires proof that every frame is unique -- a file can be the right length and still be full of duplicates.

### The Realtime trap (the #1 cause of juddered exports)

The **Realtime** flag (timeline bar; Python `project.realTime`, a read/write bool) is **ON by default** in TD (the default cooking mode skips frames to keep wall-clock pace; the [Project Class](https://docs.derivative.ca/Project_Class) page documents the read/write semantics, not the default). With it ON, TD **skips any frame it cannot cook within the `project.cookRate` budget** (default 60) -- [Project Class](https://docs.derivative.ca/Project_Class): *"When True, frames may be skipped in order to maintain the cookRate. When False, all frames are processed sequentially regardless of duration."* When a frame is skipped during recording, the Movie File Out **replicates the previous image** so the file stays the right length (its Info CHOP describes `last_frames_written` as possibly *"multiple repeats of the same image if TouchDesigner dropped frames"*). Net: the file has the correct frame COUNT but contains duplicated frames -- visible judder.

**Rule: before any movie render, capture the prior flag, then go non-realtime:**

```python
prior = project.realTime          # CAPTURE first -- never assume it was True
project.realTime = False          # cook every frame regardless of duration
```

TD now cooks every frame completely regardless of how long it takes -- nothing is skipped ([Movie File Out TOP](https://docs.derivative.ca/Movie_File_Out_TOP): *"Recording a movie without frame drops can be done in non-realtime by turning off the Realtime flag."*).

**Restoring Realtime is a footgun -- handle every exit path.** Restore `project.realTime = prior` (the captured value, NOT a hardcoded `True` -- the user may have deliberately had it OFF) when the render ends. With the async `run(delayFrames=...)` driver below there is NO Python `try/finally` that spans the render, so you cannot wrap it in `finally`. Route ALL exits -- last frame written, a force-cook exception, a drop/count-mismatch abort, AND user cancel -- through one `_finish(prior)` helper that sets `project.realTime = prior` and `mfo.par.record = 0`. Wrap each per-frame body in `try/except` so a mid-sequence error calls `_finish` instead of orphaning the scheduled chain and stranding TD non-realtime (which looks like a frozen UI -- the timeline runs only as fast as it cooks). If you re-enter after an interrupted render, read and restore `project.realTime` to the user's intended value BEFORE starting a new one.

### Monitor DURING the render -- abort on the first drop

Do not wait until the file is closed to discover frame 12 dropped (on a long render that wastes minutes of GPU time). Inside the per-frame driver, after each step, read the Movie File Out **Info CHOP** `total_frames_dropped` (and, on the addframe path, confirm `last_frames_written == 1` -- each pulse must write exactly one unique frame). If `total_frames_dropped` ever increments, STOP immediately, route to `_finish(prior)`, and report the offending frame index -- never let the render run to completion past the first drop. The [Perform CHOP](https://docs.derivative.ca/Perform_CHOP) `droppedframes`/`cook` channels are a cheap per-frame corroborating signal (`cook == 0` marks a skipped frame).

### Prove the render is good -- length and uniqueness are SEPARATE checks

A render can be the right length yet full of duplicates, so verify both classes:

**Length / completeness (does NOT prove zero drops):**
- `total_frames_written == requested_frame_count` (Info CHOP) and on-disk count == requested (`ffprobe -count_frames`). A correct count proves only that the file is the right LENGTH -- replicated frames are counted as written, so this can pass on a juddered file.

**Uniqueness / no drops (the actual drop proof):**
- `total_frames_dropped == 0` (Info CHOP) -- *"the number [of] frames TouchDesigner failed to provide unique images for"* -- AND
- duplicate-frame detection: `ffmpeg -vf mpdecimate` keeps every frame, or a per-frame `framemd5` (`ffmpeg -f framemd5`) shows no consecutive identical hashes. (mpdecimate with default thresholds also flags slow-but-distinct frames; `framemd5` detects only EXACT replication.)

**Let the encoder drain before verifying.** The Movie File Out encodes on a background thread; the last frames sit in a queue that must flush before the file is complete. Do NOT `project.quit()`, delete the op, or run the external `ffprobe`/`mpdecimate` check the instant the final frame is pulsed -- you may read a truncated file and misreport a drop. After the last frame, set `record = 0` to finalize, wait until the file size/mtime is stable across a couple of frames (or a bounded number of `delayFrames`), THEN run the on-disk check.

### Deterministic per-frame export (exact frame count)

For a frame-accurate offline render (e.g. an exact-loop sequence driven by a uniform):

- Capture `prior`; set `project.realTime = False`.
- Movie File Out `type = 'stopframemovie'` (or `'imagesequence'`), `pause = 1`, `record = 1`.
- For each frame `i` in `0..N-1`: set that frame's state (uniforms/params), **force-cook the source TOP and confirm it actually cooked** (`cookedThisFrame` True / `totalCooks` incremented, no cook error or GLSL fallback image), then `mfo.par.addframe.pulse()` -- Add Frame writes exactly one frame per pulse (Pause must be On to enable it).
- **Step across real frames with `run('...', delayFrames=1)`** -- a blocking Python `for` loop CANNOT advance TD frames, so the Movie File Out never writes. The driver self-schedules one step per frame and routes every exit through `_finish(prior)`.
- `TOP.save(path)` per frame also works but is ~seconds/frame (synchronous GPU readback + encode) -- too slow for long sequences; prefer the Movie File Out's threaded encoder.

`performLongOperation` is NOT a documented Project/UI method -- do not rely on it. Use `project.realTime = False` + `run(delayFrames=...)` chunking.

### If drops happen, the fix depends on the mode

- **Realtime recording (Realtime ON) dropped frames** -> set `project.realTime = False` and re-render.
- **Deterministic / non-realtime path dropped frames** -> Realtime is already OFF, so "re-render with realtime off" is a no-op. The cause is elsewhere: the source TOP was not force-cooked to completion before the pulse, a GLSL/compile error produced the fallback image, or the encoder stalled. Confirm each frame the source cooked uniquely (`cookedThisFrame` True, `totalCooks` incremented, no errors, `last_frames_written == 1`) before pulsing.

## Diagnosing CPU vs GPU Bottleneck

Per [Optimize](https://docs.derivative.ca/Optimize), if dropping render resolution to 64x64 does not raise fps, the bottleneck is CPU, not GPU. A Null/In/Out op with a large `cpuCookTime` or `childrenCPUCookTime` signals a CPU-overload cook cascade.

If TD has already crashed, it writes a `CrashAutoSave.<project>.toe` that opens in a safe, bypassed mode.
