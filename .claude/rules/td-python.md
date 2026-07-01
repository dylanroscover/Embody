# TouchDesigner Python Rules

Essential rules to prevent common mistakes. For full API reference, use the `/td-api-reference` skill.

## Verify Before Claiming

- **Never assume a TD feature, file type, or convention exists** without confirming against official Derivative documentation (docs.derivative.ca). This applies to Python API calls AND to claims about TD application behavior, file formats, default directories, or generated artifacts.
- **When in doubt, search first** — use WebSearch with `allowed_domains: ["derivative.ca", "docs.derivative.ca", "forum.derivative.ca"]` to verify.

## Parameters

See `parameters.md` for all parameter rules — reading/writing values, designing custom parameter pages, help text, sections, and naming.

## Naming — Methods, Functions, Operators

**Name things for what they do, not how they do it.** A reader seeing only the name should know what to expect. If you can't describe the behavior in the name, the method is probably doing too much — split it.

- **Prefer intent verbs** — `EnsureCatalogs()`, `RestoreSettings()`, `RebuildIndex()`. "Ensure X" means "make X true, doing whatever is needed." Standard, self-explanatory.
- **Avoid vague pairs** — `CheckAndX()`, `DoStuff()`, `Process()`, `Handle()`, `Manage()`. `CheckAndScan()` tells the reader nothing about *what* is checked or scanned. Rename to the outcome: `EnsureCatalogs()`.
- **Avoid implementation leakage** — `ParseJSONAndUpdateTable()` exposes internals that should be free to change. Pick a name describing the *effect*: `RefreshOpList()`.
- **Don't abbreviate domain terms** — `CalcTDNFp()` is cryptic; `ComputeTDNFingerprint()` reads instantly. Screen space is cheap; comprehension is not.
- **Booleans read as questions** — `isPaletteClone()`, `hasExternalWires()`, `canExportDAT()`. Not `paletteCheck()` or `wiresState()`.
- **Public vs private** — TD extension methods promoted to the COMP are UpperCamelCase (`EnsureCatalogs`, `Update`); internal helpers are `_lowerCamelCase` (`_loadBootstrapPalette`). Keep the public surface minimal and obviously-named.
- **Operator names: `optype_name` for processing ops, role name for the rest.** Prefix data-flow operators (TOP/CHOP/SOP/MAT/POP) with their op type so the network self-documents -- `glsl_colorize`, `noise_terrain`, `feedback_state`, `blur_soften` (the suffix still names the role; `glsl_colorize` > `glsl1`). **Exempt from the prefix:** (a) COMPs reached by a parent/global shortcut or holding an extension -- the shortcut *is* the name (`Embody`, never `comp_embody`); (b) a contract-fixed terminal, e.g. a specimen's output is an Out TOP named `out1`; (c) DATs, which stay role-named (`tdn_exporter`, not `text_tdn_exporter`; a `json`-named DAT also shadows stdlib). The network should still read like prose.

When in doubt: write the one-line docstring *first*. If the name isn't already in that docstring, the name is wrong.

## Operator Access

- **Use `opex()` when the operator must exist** — raises immediately with a clear error. `op()` returns `None` silently.
- **Never call `op()`, `parent()`, or access TD objects at module level**. Module-level code executes during import, before the network is ready. Defer to methods.
- **DAT naming conflicts**: TD searches for DATs by name before `sys.path`. A DAT named `json` shadows Python's stdlib `json`. Name DATs carefully.

## Operator Referencing

How you reference an operator matters. A wrong choice works today and breaks tomorrow — when the component is renamed, instanced, or moved. The goal is always to pick the **narrowest, most portable reference** that correctly resolves from where the code runs.

**Absolute paths are always wrong — in code, expressions, AND parameter values.** `op('/embody/Embody/...')` or `op('/project1/...')` hardcodes the entire network hierarchy. The moment anything is renamed, relocated, or instanced, it breaks. If you see a `/` at the start of an operator path anywhere — an `op()` call, a parameter expression, or a `set_parameter` value — it's a bug.

### Relative Paths — for operators near you

Use relative paths when the target operator is in the same network or a nearby one. These are the simplest and most portable references because they describe relationships, not locations.

- `op('sibling_name')` — another operator in the same network (same parent COMP).
- `op('./child_name')` — an operator inside `me` (only valid from a COMP).
- `op('../sibling_of_parent')` — an operator in the parent's network (go up one level, then find by name).

Relative paths break down when you need to reach across distant parts of the network. That's where shortcuts come in.

### Parent Shortcuts (`parent.CompName`) — for reaching your owner

A Parent Shortcut is set on a COMP's Common page via the `parentshortcut` parameter. Once configured, any operator that is a descendant of that COMP (child, grandchild, etc.) can reference it as `parent.CompName`. TD resolves this by walking up the parent chain from the caller until it finds a COMP whose Parent Shortcut matches.

This is the right choice when code running **inside** a component needs to reach the component itself — typically to call extension methods or navigate relative to the component root.

- `parent.Embody.Update()` — call a promoted extension method.
- `parent.Embody.ext.Embody.helperMethod()` — reach a non-promoted method.
- `parent.Embody.op('subpath/op_name')` — navigate from the component root to find an internal operator.

Key properties:
- **Reusable across instances**: Multiple COMPs can use the same Parent Shortcut name. Each descendant resolves to its own nearest matching ancestor — so the same code works identically across every instance of a component.
- **Only resolves from inside**: Code that is not a descendant of the COMP will not find it via `parent.CompName`. This is a feature, not a limitation — it keeps references scoped to where they belong.
- **Not the same as `parent()`**: `parent()` always returns the immediate parent COMP. `parent.CompName` searches upward by name and can skip multiple levels.

### Global OP Shortcuts (`op.CompName`) — for project-wide access

A Global OP Shortcut is set on a COMP's Common page via the `opshortcut` parameter. It registers the COMP so that `op.CompName` resolves to it from **anywhere** in the project.

This is the right choice for singleton services that many unrelated parts of the project need to reach — logging, test runners, shared managers.

- `op.Embody.Log('message')` — call Embody's logging from anywhere.
- `op.unit_tests.RunTests()` — kick off the test runner from any script.

Key properties:
- **Globally unique**: Only one COMP can hold a given Global OP Shortcut name at a time. Assigning a name already in use removes it from the previous holder.
- **Use sparingly**: If `parent.CompName` works, prefer it. Global shortcuts create invisible coupling — any code anywhere can depend on the name existing, making renames and refactors risky.

### Choosing the right reference

Think about **where the calling code lives** relative to the target:

| Relationship | Pattern | Why |
|---|---|---|
| Same network (siblings) | `op('name')` | Simplest. No hierarchy traversal. |
| Inside your own COMP | `op('./child')` | Reaches your children explicitly. |
| Up one level | `op('../name')` | Reaches parent's siblings. |
| Anywhere inside a component | `parent.CompName` | Scoped to descendants. Supports instancing. |
| Anywhere in the project | `op.CompName` | Global singleton access. Use only when parent shortcut can't work. |

**Always verify references resolve correctly.** After writing an expression or script that uses `op()`, `parent.X`, or `op.X`, confirm the target exists and the path resolves from the calling context. A reference that returns `None` silently (or worse, finds the wrong operator) is a latent bug.

## Extensions

- **`extensionsReady` guard**: Parameter expressions referencing extension-promoted attributes must use: `parent().MyProp if parent().extensionsReady else 0`
- **Auto-reinitializes on source change**: Implement `onDestroyTD(self)` for clean teardown. Use `onInitTD(self)` for post-init setup.

### `onInitTD` and TDN Import Timing

**Any initialization that sets up state inside a TDN-strategy COMP will be destroyed when TDN import runs.** TDN reconstruction (`ReconstructTDNComps`) calls `ImportNetwork` with `clear_first=True`, which deletes all children and recreates them from the `.tdn` file. If an extension's `onInitTD` creates operators, sets parameters, stores values, or builds internal state inside a TDN COMP, that work is wiped out by the import.

This applies to:

- **Project open**: `ReconstructTDNComps` runs at frame 60. Extensions inside TDN COMPs initialize earlier (when the COMP shell is created), so `onInitTD` fires before the import overwrites everything.
- **Ctrl+S / `project.save()`**: The strip/restore cycle deletes children pre-save, then re-imports them post-save. Extensions reinitialize after the restore, but the import may still be completing.

**Rules:**

1. **Defer initialization that depends on network state.** Use `run('self.mySetup()', delayFrames=5)` in `onInitTD` so the setup executes after the TDN import completes. The delay must be long enough for all import phases to finish.
2. **Never assume `onInitTD` runs once.** Inside TDN COMPs, extensions may reinitialize multiple times: on project open, after every save (strip/restore), and on manual TDN reimport. `onInitTD` must be idempotent.
3. **Guard against missing children.** During the strip phase of a save, the COMP's children are temporarily gone. If `onInitTD` fires during this window, `op('child')` returns `None`. Always null-check operators before accessing them.
4. **Store persistent state outside the TDN boundary.** If an extension needs state that survives reimport, use `store()` on the COMP itself (storage is preserved through TDN import) or on an ancestor outside the TDN COMP.

## Threading and Background Work

**Ironclad rule (a read is treated exactly like a write).** From any thread but the main thread, NEVER touch a main-thread-owned TD object: `op()`/`opex()`, a `Par`/`ParGroup` (read OR write, including `.eval()`/`.val` on a live parameter), DAT/CHOP/SOP/TOP content, `storage` (`fetch`/`store`), `tdu.Dependency` (setting `.val` recooks on the main thread), or `debug()`/`print()` (they route to the Textport / a DAT). **Never call `run()`/`td.run()` from a worker** - it raises `tdError`; this is exactly what froze TD in the field. A worker may use ONLY: pure Python (`math`, `json`, `requests`), `tdu` math/value utilities (`tdu.clamp`/`remap`/`Vector`/`Matrix` - they do not reference TD data), parameter VALUES evaluated on the main thread and passed in, `queue.Queue`, `threading.Event`/`Lock`, `td.isMainThread()` as a guard, and the Thread Manager's `InfoQueue`/`Get/Set*Safe`/`SafeLogger`. Resolve every op path and value on the main thread BEFORE spawning the worker; the worker returns plain data for a main-thread callback to apply.

**Do NOT reach for threading first.** Match the rung to the problem TYPE (these are routes, not a strict escalation); threading is the LAST resort:

0. **Prototype synchronously** to prove the URL/auth/parse - one-shot only, short explicit timeout, never in a per-frame callback or on project open, never shipped.
1. **Fast, TD-only, no I/O -> run it inline.** Any network/disk/subprocess call is NEVER this step (latency is unbounded).
2. **Fetch data -> a native TD I/O operator, NOT Python threading.** HTTP one-shot or streaming -> **Web Client DAT**: `op('webclient1').request(url, 'GET', timeout=8000)` returns a connection id immediately and never blocks the frame; the `onResponse` callback fires on the MAIN thread, so write the result there. Parse with a **JSON DAT** (or `dat.jsonObject`) and bridge numbers to channels with **DAT to CHOP** (there is no Web Client CHOP). `ws://` -> WebSocket DAT; inbound/host -> Web Server DAT; control -> OSC; files -> File In / Folder DAT.
3. **Long main-thread (TD-touching) work -> chunk with `run(delayFrames=N)`**, each chunk small enough to fit one frame. `run()` controls WHEN, not HOW MUCH; it is not a thread and is main-thread-only (a single heavy parse deferred with `run()` still blocks whatever frame it lands on).
4. **Blocking pure-Python work (custom auth, file/disk, subprocess, heavy CPU) -> the Thread Manager.** Prefer the Palette **Thread Manager Client**; advanced: `op.TDResources.ThreadManager` + a `TDTask` whose `target` touches ZERO TD objects, applying results only in its main-thread hooks. Never call `EnqueueTask()` from a worker (ThreadManager is itself a TD COMP).
5. **Long-lived server/loop -> ThreadManager `standalone=True`** (Envoy's own MCP server, drained by its `RefreshHook` on the main thread) **or a top-level `threading.Thread`** that touches ZERO TD objects, never calls `run()`, and hands results to a `queue.Queue` drained every frame by a main-thread callback (an Execute DAT `onFrameStart` or a ThreadManager `RefreshHook`). A worker spawning a `run()`-calling sub-thread is the crash, not a rung.

Engine COMP / TouchEngine offloads heavy COOKING to a separate process (TOP/CHOP/DAT I/O only) - never use it for an I/O fetch. Stock `asyncio` blocks the frame loop; a worker-hosted loop still needs zero TD access and a queue handoff.

**Triggers.** Prefer a user-driven Pulse parameter (`onPulse` / `Par.pulse()`) for a one-shot/manual fetch, or a **Timer CHOP** for genuine intervals (fire one request per tick). Never a `sleep` loop, a self-rescheduling `run()` poller, or an auto-fetch on project open unless asked; do not start a new request while one is still pending.

**Gates.** Do not pre-optimize: a synchronous fetch that does not measurably drop a frame may not need anything above Step 2 (measurement decides whether a callback needs chunking or a worker - it never makes shipped blocking I/O acceptable on the main thread). After wiring, verify with primary evidence: `get_project_performance` shows fps/frameTime held vs baseline and `droppedFrames` flat, AND the result actually arrived (read the DAT/CHOP back; branch on `statusCode['code']` - a callback that never fires leaves TD running but empty).

For code patterns (Web Client DAT example, Thread Manager Client, polling, large payloads), load `/td-api-reference`.

## Cook Model

- **Pull-based**: Operators only cook when downstream demands output. Parameter changes make nodes dirty but don't trigger immediate cooks.
- **Always-cook operators**: Output nodes and Render TOPs cook every frame regardless.
- **Time-dependent ops cook only when demanded.** An op that references time (an `absTime` parameter expression, a Feedback TOP, anything clock-driven) is *flagged* to cook every frame -- but it still only cooks when something pulls its output: a viewer, a Render/Out TOP, a displayed COMP, or a force-cook. With nothing demanding it, a correctly-built animated network sits frozen on its last cooked frame. This is the #1 cause of "my network isn't animating" -- the chain is right but undemanded. View the terminal op (or drive it) to run it in a sandbox; cook N frames before baking a thumbnail.
- **`cook(force=True)` does NOT advance a feedback loop within a frame.** A Feedback TOP captures its target on frame boundaries, so force-cooking the chain repeatedly inside one synchronous Python loop returns the *same* state each time (`totalCooks` may not even increment). Evolution needs real frames to pass with the chain demanded -- drive it with `run(..., delayFrames=1)` or an Execute DAT `onFrameStart`, never a `for` loop.
- **Animate cheaply: static source + cheap downstream.** A heavy generator (high-octave fBm, large feedback sim) cannot re-render every frame at high resolution. Make it *static* (remove every time reference so it cooks once and caches) and put the motion in a cheap downstream op -- animate the *sampling* (drift/rotate/warp the read coordinates), not the source. Verify with `cookedThisFrame`: the source reads `False`, the animated op `True`.

## Storage and Dependencies

- **`fetch()` searches UP the parent hierarchy** by default — pass `search=False` for local-only lookup.
- **`store()` triggers recooks** — not a passive dict assignment.
- **`tdu.Dependency`**: Assign to `.val` (`dep.val = 5`), NOT the object itself (`dep = 5` destroys it). Call `.modified()` after mutating contents.

## Module Access

- **`mod.name` re-resolves** the DAT lookup every call — cache in a variable for loops: `m = mod.utils; m.func()`
- **`debug()` over `print()`** — includes source DAT name and line number automatically.

## Operator Operations

- **`changeType()` returns new op** — capture it. The original reference becomes invalid.
- **`copyOPs([list])` preserves connections** between copied operators. `COMP.copy()` does not.
- **`addError()`/`addWarning()` only works in cook callbacks** — use `addScriptError()` from extension methods.
- **`TOP.sample()` downloads entire texture** from GPU — never use in loops. Use `numpyArray()` for batch access.

## Render Coordinate System

TouchDesigner's render and texture coordinate system places **(0, 0) at the bottom-left, with Y increasing upward.** This is the opposite of numpy, PIL, screen pixels, and web conventions where (0, 0) is top-left and Y increases downward.

| Context | Origin | Y direction |
|---|---|---|
| `TOP.sample(x, y)` | Bottom-left | Up |
| GLSL `gl_FragCoord` | Bottom-left | Up |
| UV coordinates (0–1) | Bottom-left | Up |
| Crop/Transform TOP params | Bottom-left | Up |
| `scriptTOP` pixel writing | Bottom-left | Up |
| `TOP.numpyArray()` return | **Top-left** | **Down** |
| PIL / OpenCV images | **Top-left** | **Down** |
| Panel/widget screen coords | **Top-left** | **Down** |

- **`TOP.numpyArray()`** returns rows **top-to-bottom** (row 0 = top of image), but TD texture coordinates have y=0 at the **bottom**. Use `np.flipud(arr)` when converting between the two systems.
- **`TOP.sample(x, y)`**: `y=0` samples the **bottom** edge, not the top.
- **GLSL shaders**: `gl_FragCoord.y = 0` is the bottom edge of the render.

## Pre-Installed Packages

Commonly importable without installation: `numpy`, `cv2` (OpenCV), `requests`, `yaml` (PyYAML), `cryptography`, `attrs` (only `numpy` and `cv2` are documented as bundled; verify the rest in your build before relying on them). Auto-imported stdlib: `math`, `re`, `sys`, `collections`, `enum`, `inspect`, `traceback`, `warnings`.

**`requests` blocks the frame - see the Threading ladder above.** `execute_python`, parameter expressions, and operator/cook callbacks all run on TD's main thread, so a synchronous `requests.get(...)` (or `urllib`/`socket`, a large file read, `subprocess.run`, or a blocking DB call) freezes the whole UI/cook cycle for the round-trip - on a slow endpoint it can hang TD or exceed the 30s MCP timeout. `requests` has no default timeout; always pass `timeout=(connect, read)` in seconds. To fetch data, use the Web Client DAT (async, never blocks); if you must use `requests`, run it in a Thread Manager worker.
