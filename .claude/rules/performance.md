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

## Movie export

Recording or exporting ANY movie/image sequence -> MUST load /movie-export FIRST -- the default Realtime flag silently produces duplicate-frame judder, and async file readers serve stale frames that pass every container check.

## Diagnosing CPU vs GPU Bottleneck

Per [Optimize](https://docs.derivative.ca/Optimize), if dropping render resolution to 64x64 does not raise fps, the bottleneck is CPU, not GPU. A Null/In/Out op with a large `cpuCookTime` or `childrenCPUCookTime` signals a CPU-overload cook cascade.

If TD has already crashed, it writes a `CrashAutoSave.<project>.toe` that opens in a safe, bypassed mode.
