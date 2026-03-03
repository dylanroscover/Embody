# Embody + Envoy

## Project Overview

**Embody** is a TouchDesigner extension that automates externalization of COMP and DAT operators to version-control-friendly files (.tox, .py, .json, .xml, etc.). It solves the problem of TouchDesigner's binary .toe files being impossible to diff/merge in git.

**Envoy** is an MCP (Model Context Protocol) server embedded inside Embody that lets Claude Code create, modify, connect, and query TouchDesigner operators programmatically — plus manage Embody externalizations.

## Network Layout Conventions

Clean, readable operator networks are critical. Every operator placed via MCP must follow these layout rules — messy networks are never acceptable.

### Grid and Spacing

- **200-unit grid**: All operator positions must snap to multiples of 200 (e.g., `x=0, 200, 400, …` and `y=0, -200, -400, …`). Never place operators at arbitrary coordinates.
- **300 units horizontal** between connected operators in a chain (signal flow spacing).
- **400+ units vertical** between unrelated groups or parallel chains. Groups must have clear visual separation — never let different functional groups touch or overlap.

### Signal Flow

- **Left to right**: Inputs on the left, outputs on the right. Every chain of connected operators should flow horizontally left-to-right.
- **Branches split vertically**: When a signal branches (one output feeding multiple downstream operators), the branches fan out vertically from the branch point, each continuing left-to-right.

### Grouping and Annotations

- **Every logical group gets an annotation**: Use `create_annotation` (annotate mode with a title) around each cluster of related operators. Not just major sections — every distinct functional group.
- **Annotations must enclose their operators**: An annotation is a visual container — it must fully surround all operators it describes. After placing operators in a group, calculate the annotation's position and size to encompass them with padding (~100 units on each side). Use `get_op_position` on all operators in the group to find the bounding box, then `set_annotation` or `create_annotation` with `x`, `y`, `width`, `height` that cover the full extent plus padding. An annotation that sits beside or above its operators instead of around them is wrong.
- **Spatial proximity for sub-groups**: Within an annotation group, related operators should be near each other with consistent internal spacing.
- **Clear group boundaries**: Leave at least 400 units between the edges of annotation groups.

### Operator Placement Rules

- **Never rely on `layout()`**: TD's built-in `COMP.layout()` method arranges children with no awareness of relationships, producing overlapping, unreadable layouts. Claude must calculate and set explicit `x, y` positions using `set_op_position`. `layout()` may be used sparingly as a starting point for throwaway or temporary networks, but production networks must always have intentional, explicit positioning.
- **New operators go near related operators**: When adding an operator to an existing network, place it adjacent to the operator(s) it relates to — not at `[0, 0]` and not at the bottom of the network.
- **Scan before placing**: Before placing new operators, use `get_op_position` on nearby operators to understand the existing layout, then calculate positions that maintain the grid and spacing conventions.
- **Align rows and columns**: Operators at the same stage in a chain should share the same X coordinate. Parallel chains should share the same Y coordinate. Use consistent alignment, not approximate eyeballing.

### Positioning Strategy for MCP Operations

When creating operators via MCP:

1. **Single operator**: Use `get_op_position` on the parent/related operator, calculate the new position at a grid-snapped offset (typically +300 horizontally for a downstream connection, or +/-400 vertically for a parallel chain), then `set_op_position`.
2. **Multiple operators in a chain**: Calculate all positions upfront based on chain length × 300 horizontal spacing, place them all, then connect.
3. **Bulk creation (10+ operators)**: Calculate a grid layout based on logical grouping, set all positions explicitly, then add annotations around each group.
4. **Adding to existing networks**: Read positions of existing operators first, find open space that maintains the grid, and place new operators without disrupting the existing layout.

### Y-Axis Convention

TouchDesigner's Y-axis increases upward. New rows of operators should go **downward** (decreasing Y) from existing ones. The first/primary chain is at the top (highest Y), with secondary chains below.

### Annotation Coordinate Model

Annotations (`annotateCOMP`) use `nodeX`/`nodeY` as their **bottom-left corner**, with `nodeWidth`/`nodeHeight` extending **rightward and upward**:

- Annotation rectangle covers: X from `nodeX` to `nodeX + nodeWidth`, Y from `nodeY` to `nodeY + nodeHeight`
- The title bar renders at the **top** of this rectangle (highest Y)
- An operator at `[op_x, op_y]` with size `[op_w, op_h]` is enclosed when its entire tile fits inside the annotation's rectangle

**To enclose a group of operators:**
1. Find the bounding box of all operators: `min_x`, `max_x`, `min_y`, `max_y` (where max includes operator width/height: `max_x = max(op_x + op_w)`, `max_y = max(op_y + op_h)`)
2. Add padding: 70 units on left/right/bottom, **170 units on top** (to leave room for the title bar and body text)
3. Set: `nodeX = min_x - 70`, `nodeY = min_y - 70`
4. Set: `nodeWidth = max_x - min_x + 140`, `nodeHeight = max_y - min_y + 240` (70 bottom + 170 top)

**Top padding rule**: The title bar and body text render at the **top** of the annotation rectangle. Without extra top padding (~170 units), operators in the first row will overlap the text. Always add more space above the highest operator row than below the lowest.

**Common mistake**: Setting `nodeY` to a value above the operators (e.g., `nodeY = max_op_y + offset`) — this places the annotation's bottom edge above the operators, so they appear below/outside the annotation. The `nodeY` must be **below** (less than) the lowest operator's Y position.

**`annotateCOMP` quirks:**
- `utility` property: `True` when created via TD UI (hidden from `.children`, only found with `findChildren(includeUtility=True)`); `False` when created programmatically (appears in `.children`). When creating annotations programmatically, set `ann.utility = True` to match TD UI behavior.
- `.type` returns `'annotate'` (not `'annotateCOMP'`)
- `findChildren(type=annotateCOMP)` requires the class object, not the string `'annotateCOMP'`
- Cannot be reliably renamed after creation — TD may ignore name assignments

## TouchDesigner Development

- TouchDesigner **auto-reinitializes extensions** when their source DATs change (including externalized `.py` files synced from disk). However, old extension instances may linger in memory due to Python garbage collection issues (circular references, cached callbacks, etc.). To ensure clean teardown, implement `onDestroyTD(self)` in your extension class — TD calls it on the old instance before reinitializing. For post-init setup that needs a fully-cooked network, use `onInitTD(self)` (called at end of the frame the extension initialized). See: https://docs.derivative.ca/Extensions#Gotcha:_extensions_staying_in_memory_-_Solution:_onDestroyTD
- When working with TouchDesigner parameters, prefer `par.name` for parameter identification.
- **Toggle parameters** use `0`/`1` (not `"True"`/`"False"`). When setting a toggle via `set_parameter`, pass `value="0"` or `value="1"`.

### POPs — GPU-Accelerated Point Operators

POPs (**Point Operators**) are a new operator family in TouchDesigner 2025 that process 3D geometry data on the GPU. They are analogous to SOPs but GPU-accelerated, enabling high-performance operations on points, primitives, and vertices. POPs output data via the Render TOP or to external systems (DMX, LED, lasers).

**Key differences from SOPs:**
- All computation runs on the GPU — data downloads to CPU are explicit and can stall the pipeline
- Use `delayed=True` on data access methods (`numPoints()`, `points()`, `bounds()`) to avoid GPU stalls
- POP-specific attributes: `pointAttributes`, `primAttributes`, `vertAttributes`, `dimension`
- Use `reallocate()` to force GPU buffer reallocation

**Common POP types** (90+ available): `gridPOP`, `noisePOP`, `transformPOP`, `particlePOP`, `spherePOP`, `linePOP`, `mergePOP`, `nullPOP`, `selectPOP`, `mathPOP`, `cachePOP`, `fileinPOP`, `glslPOP`, `deletePOP`, `sortPOP`, `copyPOP`, `switchPOP`, `feedbackPOP`, `trailPOP`, `sprinklePOP`

**Python type names** follow the same convention as other families: `gridPOP`, `noisePOP`, etc. Use these with `create_op` or `parent.create(gridPOP, 'grid1')`.

```python
# Creating a POP
grid = parent.create(gridPOP, 'grid1')

# GPU-safe data access (avoid stalls with delayed=True)
n = pop_op.numPoints(delayed=True)  # Non-blocking, returns previous frame's result
pts = pop_op.points('P')             # Download point positions (blocks GPU)
bounds = pop_op.bounds(delayed=True)  # Non-blocking bounds query

# Checking attributes
attrs = pop_op.pointAttributes  # Set of point attribute names
```

- Docs: https://docs.derivative.ca/POP
- Python class: https://docs.derivative.ca/POP_Class

### `run()` — Delayed Code Execution

The `run()` function is essential for deferring Python execution in TouchDesigner. Use it whenever code needs to execute after a delay or at end-of-frame (e.g., after a cook cycle completes, after UI updates, or to avoid reentrancy issues).

```python
# Delay execution by frames or milliseconds
run("op('/project1/base1').cook(force=True)", delayFrames=1)
run("print('done')", delayMilliSeconds=500)

# End-of-frame execution (runs after current frame finishes cooking)
run("op.Embody.Update()", endFrame=True)

# Pass a callable with arguments
run(myFunction, arg1, arg2, delayFrames=5)

# Run relative to a specific operator
run("me.cook(force=True)", fromOP=op('/project1/base1'), delayFrames=1)
```

Key parameters: `delayFrames`, `delayMilliSeconds`, `endFrame=True`, `fromOP`, `group` (for batch cancellation via `td.runs`).

- Docs: https://docs.derivative.ca/Td_Module#Methods
- Tutorial: https://derivative.ca/community-post/tutorial/using-run-delay-python-code/66947

### Thread Manager — Background Tasks Without Stalling TD

Long-running Python (network requests, file I/O, MCP servers) must run in background threads to avoid freezing TouchDesigner's UI/cook cycle. The Thread Manager (`op.TDResources.ThreadManager`) wraps Python's `threading` with TD-safe hooks.

**CRITICAL: Never access TouchDesigner objects (OPs, COMPs, parameters) from a worker thread.** All TD operations must go through hooks that execute on the main thread.

```python
# Create a task
task = op.TDResources.ThreadManager.TDTask(
    target=my_background_function,       # Runs in worker thread (no TD access!)
    args=(arg1, arg2),                   # Passed to target
    SuccessHook=on_success,              # Main thread — called when target returns
    ExceptHook=on_error,                 # Main thread — called on exception
    RefreshHook=on_refresh,              # Main thread — called every frame while running
)

# Enqueue it (runs in worker pool)
op.TDResources.ThreadManager.EnqueueTask(task)

# Or run in a dedicated thread (outside the pool)
op.TDResources.ThreadManager.EnqueueTask(task, standalone=True)
```

**Key concepts:**
- `TDTask`: Unit of work with a `target` callable and optional hooks
- `RefreshHook`: Called every frame on main thread — use to process data from `InfoQueue` (the thread-safe channel from worker → main)
- `InfoQueue`: Each `TDThread` has one; `worker_thread.InfoQueue.put(data)` sends data that arrives as the `info` arg in `RefreshHook`
- `standalone=True`: Dedicated thread outside the worker pool (use for long-lived tasks like servers)
- Worker pool defaults to 4 threads (capped by CPU count)
- **ThreadManager is a TD COMP**: `op.TDResources.ThreadManager` lives at `/sys/TDResources/threadManager`. Calling `EnqueueTask()` from a worker thread triggers a THREAD CONFLICT because it accesses a TD component. For sub-tasks or background loops inside worker threads, use plain `threading.Thread` instead.

- Docs: https://docs.derivative.ca/Thread_Manager
- API: https://docs.derivative.ca/ThreadManager_Ext

### Pull-Based Cook Model

TouchDesigner uses a **pull-based** cook system — operators only cook when something downstream demands their output (a visible viewer, an output device, or an explicit `cook()` call). Changing a parameter makes the node "dirty" but does NOT trigger an immediate cook. This means downstream operators may not reflect parameter changes until the next frame or until explicitly pulled.

- **Always-cook operators**: Output nodes (Movie File Out TOP, Audio Device Out CHOP, etc.) and Render TOPs cook every frame regardless
- **Performance implication**: Nodes with no viewer and no downstream output skip cooking entirely — minimize visible viewers to reduce load
- Docs: https://docs.derivative.ca/Cook

### Parameter Access Patterns

Always use `.eval()` to get a parameter's current runtime value, regardless of its mode (constant, expression, export, bind). Using `.val` only returns the constant-mode value, which may differ from the actual runtime value.

```python
# CORRECT — always use .eval() for the current runtime value:
value = op('geo1').par.tx.eval()

# WRONG — .val only works in constant mode, returns stale/wrong value otherwise:
value = op('geo1').par.tx.val

# Setting values (both equivalent):
op('geo1').par.tx = 5
op('geo1').par.tx.val = 5  # Also implicitly sets mode to constant

# CAUTION: Setting .val implicitly switches mode to CONSTANT.
# If the par was in expression mode, the expression is now GONE:
op('geo1').par.tx.val = 5  # Silently kills any active expression

# Menu parameters accept both string name and index:
op('geo1').par.xord = 'trs'   # by name
op('geo1').par.xord = 5       # by index

# Type casting — direct method calls require explicit .eval():
me.par.tx.eval().hex()  # CORRECT
me.par.tx.hex()         # WRONG — parameter objects don't have .hex()
```

### Creating Custom Parameters

Custom parameters are created via `appendCustomPage()` on COMPs. All `append*` methods return a **ParGroup** (tuple-like), not a single Par — always index with `[0]` for single-value parameters.

```python
page = comp.appendCustomPage('Controls')
pg = page.appendFloat('Speed', label='Speed')  # Returns ParGroup, NOT Par
p = pg[0]                                       # Get the actual Par
p.default = 0.5
p.normMin = 0; p.normMax = 2    # Slider range
p.min = 0; p.clampMin = True    # Hard clamp

# Other append methods:
page.appendInt('Count')
page.appendToggle('Active')
page.appendStr('Label')
page.appendMenu('Mode')        # Creates EMPTY menu — must set .menuNames/.menuLabels separately
page.appendPulse('Reset')
page.appendRGB('Color')        # Creates Color1r, Color1g, Color1b
page.appendXYZ('Pos')          # Creates Pos1, Pos2, Pos3
page.appendOP('Target')
page.appendFile('Path')

# Cleanup:
comp.destroyCustomPars()       # Remove ALL custom pars
par.Speed.destroy()            # Remove a single custom par
```

**Naming rule:** First letter MUST be uppercase, rest lowercase/numbers. No underscores. TD enforces this.

- Docs: https://docs.derivative.ca/Custom_Parameters

### `op()` vs `opex()`

`op()` returns `None` silently when an operator is not found. `opex()` raises an exception immediately with a clear error message. Prefer `opex()` when the operator must exist — it prevents hard-to-debug downstream `NoneType` errors.

```python
# op() returns None if not found — silent failure:
node = op('/nonexistent/path')
node.par.tx = 5  # AttributeError: 'NoneType' has no attribute 'par'

# opex() raises an exception immediately:
node = opex('/nonexistent/path')  # Raises tdError with clear message

# ops() returns a LIST of all matching operators (supports wildcards):
all_noises = ops('noise*')  # [noise1, noise2, noise3, ...]
```

Use `op()` only when `None` is an acceptable/expected result (e.g., checking if an operator exists).

### `debug()` vs `print()`

Use `debug()` instead of `print()` for debugging. It automatically prefixes output with the source DAT name and line number, making it far easier to trace in complex networks with many scripts.

```python
debug('value is', x)  # Output: "myScript line 42: value is 42" (with source info)
print('value is', x)  # Output: "value is 42" (no source info)
```

### Module-Level Code Hazard

Never call `op()`, `parent()`, or access any TD objects at the top level of a `.py` file. When a DAT's content changes, TD recompiles the module, and module-level `op()` calls execute during import — potentially before the operator network is ready.

```python
# WRONG — op() at module level, executes during import:
my_op = op('base1')  # May be None during init

class MyExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

# CORRECT — defer all op() calls to methods:
class MyExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    def doSomething(self):
        my_op = op('base1')  # Resolved at call time
```

### Import Shadowing

TouchDesigner searches for DATs with a matching name before checking `sys.path`. A DAT named `json` will shadow Python's stdlib `json` module. A DAT named `os` will shadow the `os` module. Name DATs carefully to avoid conflicts with Python built-ins.

### `mod()` for Module Access

The `mod` object accesses DAT modules without `import` statements — essential in parameter expressions where `import` is not allowed.

```python
# In a parameter expression (import not available):
mod.utils.myFunction()

# In a script (three methods, decreasing performance):
import utils                         # Fastest — cached after first import
m = mod.utils; m.func()              # OK — cache the reference
mod.utils.func()                     # Slowest — re-resolves DAT lookup every call

# Access by path (works outside search path):
mod('/project1/utils').myFunction()

# Direct module property on any DAT:
op('myDat').module.myFunction()
```

**Gotchas:**
- `mod.name` re-resolves the DAT lookup every call — cache it in a variable for loops
- No package support — `import mypackage.submodule` does not work with DATs
- Search priority: current component first, then `local/modules` walking up the hierarchy
- Docs: https://docs.derivative.ca/MOD_Class

### `extensionsReady` Guard Pattern

Parameter expressions that reference extension-promoted attributes must guard against initialization timing:

```python
# In a parameter expression:
parent().MyExtensionProperty if parent().extensionsReady else 0
```

Without this, TD raises "Cannot use an extension during its initialization" during the compile phase. For post-init logic that depends on other extensions or the network being fully cooked, use `onInitTD(self)`.

### Operator Storage

Every operator has a persistent `.storage` dictionary. Use `store()` / `fetch()` for data that should survive project saves. Prefer this over Python globals or external files.

```python
op('base1').store('count', 42)
val = op('base1').fetch('count', 0)  # 0 is the default if key missing
op('base1').unstore('count')

# storeStartupValue — restored on every project load (factory defaults)
op('base1').storeStartupValue('version', 1)
```

**Gotchas:**
- `fetch()` searches UP the parent hierarchy by default — pass `search=False` for local-only: `op('base1').fetch('key', 0, search=False)`
- `store()` triggers dependent operator recooks — it is NOT a passive dict assignment
- Cannot store TD operator references — store `op.path` strings instead
- For mutable objects (lists, dicts), use `DependList`/`DependDict` from `TDStoreTools` or call `.modified()` on the Dependency wrapper to trigger updates
- Docs: https://docs.derivative.ca/Storage

### `tdu.Dependency` for Reactive Values

`tdu.Dependency` wraps a value so that parameter expressions automatically recook when it changes. Essential for extension properties that drive expressions.

```python
dep = tdu.Dependency(0)
dep.val = 5          # CORRECT — triggers dependent expression recooks
dep = 5              # WRONG — destroys the Dependency object, silently breaks all expressions

# Read without creating a dependency (avoids circular re-evaluation):
current = dep.peekVal

# Mutable contents require manual notification:
dep.val = [1, 2, 3]
dep.val.append(4)    # Does NOT trigger update
dep.modified()       # Required — notifies all dependents
```

See also: `TDFunctions.createProperty()` for creating dependable properties on extension classes. Docs: https://docs.derivative.ca/Dependency_Class

### Explicit Type Conversion

TD parameters and CHOP channels auto-cast in expression contexts but remain TD objects internally. When passing values to standard Python functions, explicitly convert with `int()`, `float()`, or `str()` to avoid type-mismatch bugs. Use `repr()` to reveal the actual type if uncertain.

### `tdu` Utility Functions

The `tdu` module provides commonly-needed utilities. Use these instead of writing custom implementations.

```python
tdu.clamp(val, min, max)              # Standard clamp
tdu.remap(val, fromMin, fromMax, toMin, toMax)  # Linear remap between ranges
tdu.rand(seed)                        # Deterministic random in [0.0, 1.0)
tdu.base('noise3')                    # 'noise' — extract base name
tdu.digits('noise3')                  # 3 — extract trailing digits
tdu.validName('my op!')               # 'my_op_' — sanitize for operator names
tdu.match('noise*', ['noise1', 'c1']) # ['noise1'] — wildcard filtering
tdu.expand('A[1-3]')                  # ['A1', 'A2', 'A3'] — range expansion
tdu.tryExcept(expr, fallback)         # Inline try/except for parameter expressions
```

### DAT Cell and Text Behavior

All DAT cells are internally **strings**. Cell objects auto-cast to numbers in expression contexts, but `.val` always returns a string.

```python
n = op('table1')
n[1,2] + 1          # Works: auto-cast to number (e.g., 4)
n[1,2].val + 1      # TypeError: str + int
n[1,2] + n[1,2]     # 6 (numeric addition via auto-cast)
n[1,2].val + n[1,2].val  # "33" (string concatenation)
```

**DAT text access:**
- `dat.text` — tab/newline delimited; **strips multi-line cell content**. Use `dat.csv` for cells containing newlines
- `dat.jsonObject` — parses DAT content as JSON and returns a Python dict directly (no `json.loads()` needed)
- `dat.module` — access DAT contents as a Python module: `op('myDat').module.myFunc()`
- `dat.write(content)` — **appends** (does not overwrite)
- Docs: https://docs.derivative.ca/DAT_Class

### CHOP Channel Access

```python
ch = op('noise1')['chan1']    # Get channel by name — NO wildcard support
chs = op('noise1').chans('tx*')  # Use .chans() for pattern matching
val = ch.eval()               # Current value (channels also auto-cast to float in expressions)

# Per-sample access:
ch[0], ch[10]                 # Sample by index
ch.evalFrame(30)              # Sample at specific frame
ch.evalSeconds(1.5)           # Sample at specific time

# NumPy integration:
arr = op('noise1').numpyArray()  # Shape: (numChans, numSamples)
```

- Docs: https://docs.derivative.ca/Channel_Class

### TOP Pixel Access

`TOP.sample(x, y)` downloads the **entire texture** from GPU to CPU to read a single pixel — extremely expensive. Never use in loops or per-frame callbacks.

```python
# For single pixel (debugging only):
r, g, b, a = op('noise1').sample(x=0.5, y=0.5)

# For batch access (still downloads, but once):
arr = op('noise1').numpyArray()  # Indexed as [height, width, channels] — NOT [width, height]
```

### Pre-Installed Python Packages

These packages are available in TouchDesigner without installation: `numpy`, `cv2` (OpenCV), `requests`, `yaml` (PyYAML), `cryptography`, `attrs`. The following stdlib modules are auto-imported (no `import` needed): `math`, `re`, `sys`, `collections`, `enum`, `inspect`, `traceback`, `warnings`.

### Creating Python Files for TouchDesigner

When creating Python files that will be used in TouchDesigner (scripts, extensions, test files, callbacks), you must **ALWAYS** create the textDAT in TouchDesigner first, then externalize it using Embody. **Never** manually set the `file` and `syncfile` parameters — that is what Embody automates.

**Workflow:**
1. Create the textDAT in TouchDesigner (via MCP `create_op` or in the TD UI)
2. Write the Python code into the DAT (via MCP `set_dat_content` or edit in TD)
3. Tag the DAT for externalization using Embody (`tag_for_externalization` MCP tool or `Ctrl+Shift+T` in TD)
4. Save the externalization (`save_externalization` or `Ctrl+Shift+U`) — Embody writes the `.py` file to disk

Embody handles all the file path management, `file` parameter configuration, `syncfile` toggling, and tracking in `externalizations.tsv`. **This is the whole reason Embody exists** — never bypass it with manual file parameter setup.

```python
# Example: creating a test file via MCP
# 1. create_op(parent_path='/embody/unit_tests', op_type='textDAT', name='test_my_feature')
# 2. set_dat_content(op_path='/embody/unit_tests/test_my_feature', text='...python code...')
# 3. tag_for_externalization(op_path='/embody/unit_tests/test_my_feature')
# 4. save_externalization(op_path='/embody/unit_tests/test_my_feature')
```

## Approach Guidelines

- Before editing a file, verify it is the ACTUAL file responsible for rendering the UI element in question. Grep for the specific component/text/class being displayed and trace the render path before making changes.
- Avoid over-engineering fixes. When something works, do not refactor or add abstraction layers (e.g., snapshot mechanisms, extra caching) unless explicitly asked. Prefer minimal, targeted changes.
- When debugging, do NOT jump to conclusions about root causes. State your hypothesis, verify it with evidence (logs, grep, reading code), and only then propose a fix.

## Project Structure

```
Embody/
├── CLAUDE.md                              # This file
├── README.md                              # User-facing docs, changelog
├── LICENSE                                # TEC Friendly License v1.0
├── docs/                                  # MkDocs documentation site
│   ├── embody/                           # Embody feature docs
│   ├── envoy/                            # Envoy MCP server docs
│   ├── tdn/                              # TDN format specification
│   ├── td-development/                   # TD coding best practices
│   ├── tdn.schema.json                   # JSON Schema for .tdn validation
│   ├── testing.md                        # Test framework docs
│   └── changelog.md                      # Version history
├── dev/
│   ├── Embody-5.140.toe                    # Active development project
│   ├── .venv/                             # Python virtual environment (auto-created)
│   ├── Backup/                            # Versioned .toe backups
│   └── embody/
│       ├── externalizations.tsv           # Externalization tracking table (managed by Embody)
│       └── Embody/                        # Main extension source
│           ├── EmbodyExt.py               # Core externalization engine (~2,200 lines)
│           ├── EnvoyExt.py             # MCP server extension (~2,500 lines)
│           ├── TDNExt.py                  # TDN network format export/import (~1,500 lines)
│           ├── text_claude.md          # Template for generating per-project CLAUDE.md
│           ├── execute.py                 # Project lifecycle callbacks
│           ├── parexec.py                 # Parameter change callbacks
│           ├── keyboardin_callbacks.py    # Keyboard shortcut handlers
│           ├── timer_callbacks.py         # Timer callbacks (double-press detection)
│           ├── chopexec_exit_tagger.py    # CHOP exit handler
│           └── help/
│               └── text_help.py            # Help text
└── release/
    └── Embody-v*.tox                     # Latest release build
```

## Architecture

### Externalization Sync (.toe ↔ .py files)

TouchDesigner projects are binary `.toe` files. Embody externalizes tagged operators to text files under `dev/embody/`:

1. **Tagging**: Operators are tagged for externalization (keyboard shortcut or MCP tool)
2. **Sync out**: On `Ctrl+Shift+U` or project save, Embody writes tagged operators to files (`.tox` for COMPs, `.py`/`.json`/etc. for DATs)
3. **Sync in**: When the `.toe` is opened, TouchDesigner reads the external files back into operators via their `file` parameter
4. **Tracking**: `dev/embody/externalizations.tsv` tracks all externalized ops with path, type, timestamp, dirty state, and build number

**Important**: Edits to `.py` files in `dev/embody/Embody/` are read by TD when the project loads or the DAT syncs. Changes made inside TD are written out to these files on save. This bidirectional sync is the core of the system.

### Envoy MCP Architecture

Envoy uses a dual-thread design:

- **Worker thread**: Runs the MCP server (FastMCP with Streamable HTTP transport via uvicorn) — no TouchDesigner imports allowed
- **Main thread**: Executes all TD operations via `EnvoyExt._onRefresh()` callback
- **Communication**: `threading.Event` + `Queue` for request/response between threads
- **Thread management**: Uses `op.TDResources.ThreadManager` (TDTask pattern)
- **Graceful shutdown**: `shutdown_event` (threading.Event) signals uvicorn to exit cleanly
- **Version**: `ENVOY_VERSION` constant tracks server version for compatibility

## Architecture Notes

- **Stateless HTTP transport**: Envoy uses `stateless_http=True` because TD's single-threaded model means concurrent sessions would queue on the same main-thread execution path anyway. Stateless mode simplifies the implementation and avoids session management overhead.
- **30-second operation timeout**: `_execute_in_td()` times out at 30 seconds. This prevents indefinite hangs if the main thread is blocked (e.g., modal dialog), while allowing enough time for heavy operations like `.tox` saves. If a TD operation takes longer, the MCP tool returns a timeout error — the operation may need to be broken into smaller steps.
- **127.0.0.1 binding**: Security requirement to prevent DNS rebinding attacks. Envoy must never bind to `0.0.0.0` or be accessible from the network.
- **Standalone thread**: The MCP server runs as a `standalone=True` TDTask because it is long-lived (runs for the entire session), unlike pool tasks which are meant for short-lived work units.
- **Queue-based cross-thread communication**: Uses `threading.Event` + `Queue` rather than locks because TD's cook cycle is frame-based — the main thread can only process requests once per frame via the RefreshHook.

### Graceful Shutdown Sequence

1. `EnvoyExt.Stop()` is called (from UI toggle, `onExit`, or project close)
2. `shutdown_event.set()` signals the worker thread's uvicorn server to stop
3. Uvicorn completes its shutdown (stops accepting connections, drains existing)
4. Worker thread's target function returns
5. `SuccessHook` or `ExceptHook` fires on the main thread for cleanup
6. Port is released and available for rebinding

### TDN Network Format

TDN (TouchDesigner Network) is a JSON-based format for representing TD operator networks as human-readable, diffable text. It is implemented in `TDNExt.py` and exposed via MCP tools (`export_network`, `import_network`) and keyboard shortcuts.

**Key design decisions:**
- **Non-default only**: Only parameters whose values differ from defaults are exported, keeping files minimal
- **Expression shorthand**: Expressions use `=` prefix (e.g., `"=me.digits"`), bind expressions use `~` prefix — no wrapper objects
- **Type defaults**: Parameters shared unanimously across all operators of a type are hoisted into a top-level `type_defaults` section
- **Parameter templates**: Identical custom parameter page definitions across 2+ operators are extracted into a `par_templates` section and referenced via `$t`
- **Compact JSON**: Short arrays (position, size, color, tags, connections) are inlined to single lines
- **Flags as arrays**: `["viewer", "display"]` instead of `{"viewer": true, "display": true}`; `-` prefix for negated true-defaults
- **Page-grouped custom pars**: `custom_pars` is a dict keyed by page name, not a flat array with `"page"` on every entry
- **Simplified connections**: `["noise1"]` instead of `[{"index": 0, "source": "noise1"}]`; array position equals input index
- **Optional position**: Operators at `[0, 0]` omit the `position` field entirely
- **Pre-phase + 7-phase import**: Templates and type defaults are resolved first, then operators are created, custom parameters, parameter values, flags, connections, DAT content, and positions — in that specific order to satisfy dependencies
- **Relative source references**: Connections reference siblings by name only, falling back to full paths for cross-network references
- **Palette clone detection**: COMPs cloned from `/sys/` are marked but their children are not exported (TD recreates them automatically)

**File format**: JSON with `.tdn` extension. Full specification: [`docs/tdn/specification.md`](docs/tdn/specification.md)

**Export modes:**
- `Ctrl+Shift+E` — export entire project to a single `.tdn` file
- `Ctrl+Alt+E` — export the current COMP to a `.tdn` file
- `export_network` MCP tool — programmatic export with options for root path, depth, DAT content inclusion, and per-COMP splitting

## Extension Referencing Conventions

When referencing the Embody extension in TouchDesigner Python:

```python
# Promoted methods (uppercase) — called directly on the component
op.Embody.Update()
op.Embody.Save()
op.Embody.TagGetter()

# Non-promoted methods (lowercase) — accessed through ext
op.Embody.ext.Embody.getExternalizedOps()
op.Embody.ext.Embody.isOpEligibleToBeExternalized(someOp)
op.Embody.ext.Embody.safeDeleteFile(path)

# Envoy-specific extensions
op.Embody.ext.Envoy.Start()
op.Embody.ext.Envoy.Stop()
```

## Code Conventions

- **Extension naming**: Extension classes and their source DATs must follow the `NameExt` convention (e.g., `EmbodyExt`, `EnvoyExt`, `TDNExt`, `TestRunnerExt`). The class name should match the DAT name.
- **Renaming operators**: To rename a TD operator, ONLY rename the operator itself — via MCP `rename_op` or inside TouchDesigner. **NEVER** rename the externalized file on disk, **NEVER** manually update the `file`/`externaltox` parameter, and **NEVER** edit the externalizations table. Embody's `checkOpsForContinuity` handles everything automatically: it detects the stale path in the table, `_findMovedOp` matches the renamed operator by its file parameter, then `updateMovedOp` renames the file on disk, updates the `file`/`externaltox` parameter, and updates the table row — all in one step.
- **Logging**: Use `op.Embody.Log(message, level)` from anywhere in the project. Levels: `'DEBUG'`, `'INFO'`, `'WARNING'`, `'ERROR'`, `'SUCCESS'`. Convenience methods: `op.Embody.Debug(msg)`, `op.Embody.Info(msg)`, `op.Embody.Warn(msg)`, `op.Embody.Error(msg)`. Logs go to: FIFO DAT (TD UI), textport (if `Print` par enabled), log file (enabled by default), and ring buffer (MCP access via `get_logs` tool + auto-piggybacked on all MCP tool responses in the `_logs` field). **File logging** is enabled by default — logs are written to `dev/logs/<project_name>_YYMMDD.log` with automatic rotation at 10 MB (`_001.log`, `_002.log`, etc.). The ring buffer and piggybacked logs are limited in size; **always read the log file on disk for the complete picture**.
- **Paths**: Always use forward slashes (`/`) for cross-platform compatibility — never backslashes
- **File safety**: Only delete files tracked by Embody (`isTrackedFile()`, `safeDeleteFile()`). Never delete untracked files
- **Directory cleanup**: Use `rmdir()` only (fails on non-empty dirs) — never `shutil.rmtree()`
- **Error handling**: Wrap TD operations in try/except, return `{'error': str(e)}` dicts in MCP handlers
- **Thread safety**: Never import or call TouchDesigner modules in worker thread code (`EnvoyMCPServer` class)
- **Table management**: The `externalizations.tsv` is managed exclusively by Embody — never edit it directly
- **No `hasattr` for known parameters**: Embody's custom parameters (e.g., `Envoyenable`, `Envoyport`, `Envoystatus`) are static and locked in the `.toe` — they always exist. Do not wrap access in `hasattr(self.ownerComp.par, ...)` checks. Just use them directly (e.g., `self.ownerComp.par.Envoystatus = 'Running'`)
- **MCP error types**: Envoy handles two error categories: (1) Protocol errors (JSON-RPC level) for unknown tools, invalid arguments, or server errors — FastMCP handles these automatically. (2) Tool execution errors returned in tool results via `{'error': str(e)}` dicts — these indicate the tool ran but encountered a problem (missing operator, invalid path, etc.). Always return structured error information rather than raising exceptions in tool handlers.
- **MCP input validation**: All tool handlers must validate inputs before passing to TD operations. Check that `op_path` is a valid path format, verify operators exist before operating on them, validate parameter names, and sanitize string inputs passed to `eval()` or `exec()`.
- **Localhost binding**: Envoy must bind to `127.0.0.1`, never `0.0.0.0`. Binding to all interfaces would expose the MCP server to the local network and enable DNS rebinding attacks from malicious websites.
- **Tool signatures are MCP schema**: FastMCP generates tool definitions from function signatures and docstrings in `_register_tools()`. Changing parameter names, type hints, or docstrings changes the tool's public MCP interface. Treat these as API contracts — changes may break client integrations.
- **Never cache extension references in variables**: Always call extension methods directly inline — never store an extension reference in a local variable. Extension instances can become stale after TD reinitializes them (e.g., when an externalized `.py` changes on disk), and a cached reference will silently call methods on the dead old instance. Always use the full path every time: `self.ownerComp.ext.Embody.SomeMethod()`, `parent.Embody.ext.TDN.ExportNetwork()`, etc. The one exception is `getattr()` existence checks (e.g., `getattr(self.ownerComp.ext, 'TDN', None)`) where you guard with `if not ... return` and immediately call the live reference.

## File Editing Impact

| File | Impact | Notes |
|------|--------|-------|
| `EmbodyExt.py` | HIGH | Core engine. Changes affect all externalization behavior. |
| `EnvoyExt.py` | HIGH | MCP server. Two distinct sections: `EnvoyMCPServer` (worker thread, no TD imports) and `EnvoyExt` (main thread, TD access). Tool signature changes break client API. |
| `TDNExt.py` | MEDIUM | Network export/import. Changes affect `.tdn` format compatibility. |
| `execute.py` | LOW | Project lifecycle callbacks (`onStart`, `onProjectPreSave`, etc.). Rarely needs changes. |
| `parexec.py` | MEDIUM | Fires on every parameter change. Performance-sensitive. |
| `keyboardin_callbacks.py` | LOW | Keyboard shortcut handlers. Additive changes are safe. |
| `timer_callbacks.py` | LOW | Double-press detection logic. |
| `chopexec_exit_tagger.py` | LOW | CHOP exit handler for tagging. |
| `externalizations.tsv` | NEVER EDIT | Managed exclusively by Embody. Manual edits corrupt tracking. |
| `text_claude.md` | MEDIUM | Template for per-project CLAUDE.md. Must be kept in sync with root CLAUDE.md and text_help.py. |
| `help/text_help.py` | LOW | Help text displayed in Embody UI. Must be kept in sync with CLAUDE.md and text_claude.md for shortcuts, features, and supported formats. |

## TouchDesigner Documentation

**Always research TD features before writing code.** Even if you think you understand a feature, confirm on the wiki — assumptions about TD's Python API are frequently wrong.

- **Wiki home**: https://docs.derivative.ca/Main_Page
- **Glossary**: https://docs.derivative.ca/TouchDesigner_Glossary
- **Operator pages**: `https://docs.derivative.ca/index.php?title={OP_Name}` (e.g., `List_COMP`, `Text_DAT`, `Noise_TOP`)
- **Python class pages**: `https://docs.derivative.ca/{ClassName}_Class` (e.g., `ListCOMP_Class`, `Par_Class`, `OP_Class`)
- **Common references**:
  - https://docs.derivative.ca/OP_Class — base operator class
  - https://docs.derivative.ca/COMP_Class — component class
  - https://docs.derivative.ca/Par_Class — parameter class
  - https://docs.derivative.ca/Tdu_Module — TD utility module
  - https://docs.derivative.ca/Thread_Manager — Thread Manager for Python threading in TD (accessed via `op.TDResources.ThreadManager`)
  - https://docs.derivative.ca/POP_Class — POP (Point Operator) class — GPU-accelerated geometry
  - https://docs.derivative.ca/Extensions — Extensions system (lifecycle, promotion, `onDestroyTD`, `onInitTD`, `StorageManager`)
  - https://docs.derivative.ca/Storage — Operator storage system (store/fetch/unstore)
  - https://docs.derivative.ca/Dependency_Class — Dependency class for reactive values
  - https://docs.derivative.ca/MOD_Class — Module on Demand (mod) for DAT module access
  - https://docs.derivative.ca/Custom_Parameters — Custom parameter creation API
  - https://docs.derivative.ca/Cook — Cook cycle (pull-based evaluation model)
  - https://docs.derivative.ca/DAT_Class — DAT class (text, table, cell access)
  - https://docs.derivative.ca/Channel_Class — CHOP channel class
  - [`docs/tdn/specification.md`](docs/tdn/specification.md) — TDN network format specification

## Envoy MCP Server Setup

### Prerequisites
Embody auto-installs all dependencies (mcp>=1.2.0, pywin32>=306 on Windows) via uv when Envoy is first enabled. The virtual environment is created at `dev/.venv/` and dependencies are installed automatically.

### Enabling the Server
1. Open the Embody `.toe` project in TouchDesigner
2. Toggle the `Envoyenable` parameter ON in the Embody COMP
3. Server starts on configured port (default: 9876)

### Changing the Port
Changing the `Envoyport` parameter while the server is running will automatically:
1. Stop the server on the old port
2. Restart on the new port (after a 2-frame delay for clean shutdown)
3. Update `.mcp.json` with the new port

If the server is not running, changing the port simply updates the parameter value.

### Connecting Claude Code
Envoy auto-creates a `.mcp.json` file in the git repo root on startup. This works with both the Claude Code CLI and the VS Code extension. Just start a new Claude Code session after Envoy is running.

If you need to configure manually, create `.mcp.json` in the project root:
```json
{
  "mcpServers": {
    "envoy": {
      "type": "http",
      "url": "http://localhost:9876/mcp"
    }
  }
}
```

## MCP Tool Reference

### Operator Management

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_op` | `parent_path`, `op_type`, `name?` | Create a new operator (e.g., `baseCOMP`, `noiseTOP`, `textDAT`, `gridPOP`) |
| `create_extension` | `parent_path`, `class_name`, `name?`, `code?`, `promote?`, `ext_name?`, `ext_index?`, `existing_comp?` | Create a TD extension: baseCOMP + text DAT + extension wiring, initialized and ready to use |
| `delete_op` | `op_path` | Delete an operator |
| `copy_op` | `source_path`, `dest_parent`, `new_name?` | Copy operator to new location |
| `rename_op` | `op_path`, `new_name` | Rename an operator |
| `get_op` | `op_path` | Get full operator info (type, family, parameters, inputs, outputs, children) |
| `query_network` | `parent_path?`, `recursive?`, `op_type?`, `include_utility?` | List operators in a container. Set `include_utility=True` to include annotations |
| `find_children` | `op_path`, `name?`, `type?`, `depth?`, `tags?`, `text?`, `comment?`, `include_utility?` | Advanced search using TD's `findChildren` — filter by name pattern, type, depth, tags, text content, or comment. Set `include_utility=True` to include annotations |
| `cook_op` | `op_path`, `force?`, `recurse?` | Force-cook an operator |

### Parameter Control

| Tool | Parameters | Description |
|------|-----------|-------------|
| `set_parameter` | `op_path`, `par_name`, `value?`, `mode?`, `expr?`, `bind_expr?` | Set a parameter's value, expression, bind expression, or mode (`constant`/`expression`/`export`/`bind`) |
| `get_parameter` | `op_path`, `par_name` | Get parameter value, mode, expression, bind info, export source, label, range, menu entries, and default |

### DAT Content

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_dat_content` | `op_path`, `format?` | Get DAT text or table data (`"text"`, `"table"`, or `"auto"`) |
| `set_dat_content` | `op_path`, `text?`, `rows?`, `clear?` | Set DAT content from text string or list of row lists |

### Operator Flags

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_flags` | `op_path` | Get all flags: bypass, lock, display, render, viewer, current, expose, selected, allowCooking |
| `set_op_flags` | `op_path`, `bypass?`, `lock?`, `display?`, `render?`, `viewer?`, `current?`, `expose?`, `allowCooking?`, `selected?` | Set one or more flags on an operator |

### Operator Positioning & Layout

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_position` | `op_path` | Get operator position, size, color, and comment |
| `set_op_position` | `op_path`, `x?`, `y?`, `width?`, `height?`, `color?`, `comment?` | Set operator position, size, color (`[r,g,b]` floats 0-1), or comment |
| `layout_children` | `op_path` | Auto-layout all children in a COMP |

### Annotations

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_annotation` | `parent_path`, `mode?`, `text?`, `title?`, `x?`, `y?`, `width?`, `height?`, `color?`, `opacity?`, `name?` | Create an annotation. Modes: `"annotate"` (default, has title bar), `"comment"`, `"networkbox"` |
| `get_annotations` | `parent_path` | List all annotations in a COMP with their properties and enclosed operators |
| `set_annotation` | `op_path`, `text?`, `title?`, `color?`, `opacity?`, `width?`, `height?`, `x?`, `y?` | Modify properties of an existing annotation |
| `get_enclosed_ops` | `op_path` | Get operators enclosed by an annotation, or annotations enclosing an operator |

### Performance Monitoring

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_performance` | `op_path`, `include_children?` | Get CPU/GPU cook times, memory usage, cook counts |

### Connections

| Tool | Parameters | Description |
|------|-----------|-------------|
| `connect_ops` | `source_path`, `dest_path`, `source_index?`, `dest_index?`, `comp?` | Wire two operators together. Set `comp=True` for COMP connectors (top/bottom) |
| `disconnect_op` | `op_path`, `input_index?`, `comp?` | Disconnect an operator's input. Set `comp=True` for COMP connectors (top/bottom) |
| `get_connections` | `op_path` | Get all input/output connections (includes COMP connections for COMPs) |

### Code Execution

| Tool | Parameters | Description |
|------|-----------|-------------|
| `execute_python` | `code` | Execute Python code in TD. Set `result` variable to return values |

### Introspection & Diagnostics

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_td_info` | _(none)_ | Get TD version, build, OS, and Envoy version |
| `get_op_errors` | `op_path`, `recurse?` | Get error messages for an operator and its children |
| `exec_op_method` | `op_path`, `method`, `args?`, `kwargs?` | Call a method on an operator (e.g., `appendRow`, `cook`) |
| `get_td_classes` | _(none)_ | List all Python classes/modules in the `td` module |
| `get_td_class_details` | `class_name` | Get methods, properties, and docs for a TD class |
| `get_module_help` | `module_name` | Get Python help text for a module (supports dotted names like `td.tdu`) |

### MCP Prompts

| Prompt | Parameters | Description |
|--------|-----------|-------------|
| `search_op` | `op_name`, `op_type?` | Guide for searching operators by name |
| `check_op_errors` | `op_path` | Guide for inspecting and resolving operator errors |
| `connect_ops` | _(none)_ | Guide for wiring operators together |
| `create_extension_guide` | _(none)_ | Guide for creating TD extensions with proper patterns |

### Embody Integration

| Tool | Parameters | Description |
|------|-----------|-------------|
| `tag_for_externalization` | `op_path`, `tag_type?` | Tag operator for externalization (auto-detects type if omitted) |
| `remove_externalization_tag` | `op_path` | Remove externalization tag |
| `get_externalizations` | _(none)_ | List all externalized operators with status |
| `save_externalization` | `op_path` | Force save an externalized operator to disk |
| `get_externalization_status` | `op_path` | Get dirty state, build number, timestamp, file path |

### TDN Network Format

| Tool | Parameters | Description |
|------|-----------|-------------|
| `export_network` | `root_path?`, `include_dat_content?`, `output_file?`, `max_depth?` | Export network to `.tdn` JSON (non-default properties only) |
| `import_network` | `target_path`, `tdn`, `clear_first?` | Recreate a network from `.tdn` JSON |

**Keyboard shortcut**: `Ctrl+Shift+E` exports the entire project network to a `.tdn` file.

### Logging

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_logs` | `level?`, `count?`, `since_id?`, `source?` | Get recent log entries from ring buffer. Filter by level, source, or use `since_id` for incremental polling. |

**Auto-piggybacked logs**: Every MCP tool response includes a `_logs` field with up to 20 log entries generated since the previous tool call. Use this to monitor operations in real-time without needing to call `get_logs` separately.

**Log files on disk**: File logging is enabled by default. Logs are written to `dev/logs/<project_name>_YYMMDD.log` (e.g., `dev/logs/Embody-5.31_260212.log`). Files rotate at 10 MB with numbered suffixes (`_001`, `_002`, etc.). The ring buffer (200 entries) and piggybacked logs (20 per response) are insufficient for operations that generate many log entries (e.g., test runs, bulk externalizations). **Always read the log file after significant MCP operations** to catch errors that may have been evicted from the ring buffer.

## Common Workflows

### Creating an Operator and Verifying It
1. `query_network` on the target parent to confirm it exists
2. `create_op` with the desired type and name
3. `get_op_errors` with `recurse=true` to check for errors
4. If connecting: `connect_ops` then `get_op_errors` again

### Adding a New MCP Tool to Envoy
1. Add the tool function inside `_register_tools()` in `EnvoyExt.py`
2. Add a corresponding handler case in `_onRefresh()` for the TD operation
3. Update the MCP Tool Reference table in `CLAUDE.md`
4. Update `text_claude.md` to match
5. Test via MCP Inspector or Claude Code

### Debugging an Operator Error
1. `get_op_errors` with `recurse=true` on the suspected operator
2. `get_op` to inspect parameters and connections
3. `get_connections` to verify input/output wiring
4. `get_dat_content` if the operator is a DAT with script errors

### Externalizing an Operator
1. `tag_for_externalization` on the operator (auto-detects type)
2. `save_externalization` to force-write it to disk
3. `get_externalization_status` to confirm dirty state and file path
4. Verify file exists in `dev/embody/` via file inspection

### Creating and Managing Annotations
1. `create_annotation` with parent_path, mode (`"comment"`, `"networkbox"`, or `"annotate"`), text, and position
2. Use `get_annotations` to list all annotations in a network
3. Use `get_enclosed_ops` to see which operators a network box encloses
4. Modify text, position, or appearance with `set_annotation`

## Testing

Embody has a comprehensive automated test suite with **30 test files** covering all core functionality. The test framework lives at `/embody/unit_tests` and uses a custom test runner extension.

### Test Coverage

**Core Embody (14 suites):**
- `test_externalization` — externalization lifecycle
- `test_crud_operators` — create, read, update, delete operations
- `test_file_management` — file I/O, path handling, cleanup
- `test_tag_management` — tagging operators for externalization
- `test_tag_lifecycle` — tag application and removal
- `test_rename_move_lifecycle` — rename and move tracking
- `test_delete_cleanup` — deletion and file cleanup
- `test_duplicate_handling` — duplicate operator handling
- `test_update_sync` — sync between .toe and externalized files
- `test_path_utils` — path normalization and utilities
- `test_param_tracker` — parameter change tracking
- `test_operator_queries` — operator discovery and queries
- `test_logging` — logging system
- `test_custom_parameters` — custom parameter behavior

**MCP Tools (11 suites):**
- `test_mcp_operators` — create, delete, copy, rename, query, find
- `test_mcp_parameters` — get/set parameters, modes, expressions
- `test_mcp_dat_content` — DAT text and table operations
- `test_mcp_connections` — wiring operators together
- `test_mcp_annotations` — creating and managing annotations
- `test_mcp_extensions` — extension creation and setup
- `test_mcp_diagnostics` — error checking, performance, info
- `test_mcp_flags_position` — operator flags and positioning
- `test_mcp_code_execution` — executing Python in TD
- `test_mcp_externalization` — Embody integration via MCP
- `test_mcp_performance` — performance monitoring

**TDN Format (4 suites):**
- `test_tdn_export_import` — network export/import
- `test_tdn_helpers` — TDN utility functions
- `test_tdn_reconstruction` — reconstruction round-trip fidelity
- `test_tdn_file_io` — TDN file output, per-comp splitting, stale cleanup

**Infrastructure (1 suite):**
- `test_server_lifecycle` — Envoy MCP server start/stop

### Test Framework Features

The test runner (`TestRunnerExt`) provides:

- **Sandbox isolation** — each suite gets a fresh baseCOMP for test fixtures
- **Standard assertions** — 20+ assertion methods (assertEqual, assertTrue, assertIn, assertIsInstance, etc.)
- **Lifecycle hooks** — setUp/tearDown per test, setUpSuite/tearDownSuite per suite
- **Three execution modes:**
  - `RunTestsSync()` — synchronous, all tests in one frame (blocks TD, use for MCP)
  - `RunTestsDeferred()` — one suite per frame (keeps TD responsive)
  - `RunTestsDeferredPerTest()` — one test per frame (default, best for heavy suites)
- **Deferred execution** — uses `run()` with `delayFrames` to spread tests across frames
- **Results tracking** — table DAT with pass/fail/error/skip counts and durations
- **Dynamic module loading** — loads externalized `.py` test files with TD globals injected
- **Skip support** — `self.skip(reason)` to conditionally skip tests

### Running Tests

**From TouchDesigner:**
```python
# Run all tests (one test per frame, non-blocking)
op.unit_tests.RunTests()

# Run a specific suite
op.unit_tests.RunTests(suite_name='test_path_utils')

# Run a specific test method
op.unit_tests.RunTests(suite_name='test_path_utils', test_name='test_normalizePath_backslashes_converted')

# Run synchronously (blocks TD until complete)
op.unit_tests.RunTestsSync()

# Get results
results = op.unit_tests.GetResults()
# Returns: {'total': 156, 'passed': 156, 'failed': 0, 'errors': 0, 'skipped': 0, 'results': [...]}
```

**Via MCP:**
```python
# Using Envoy MCP tool
mcp.run_tests(suite_name='test_path_utils')  # Run one suite
mcp.run_tests()                              # Run all suites
```

**Test file location:** `dev/embody/unit_tests/test_*.py` (externalized, version-controlled)

### What Cannot Be Unit Tested

Some areas require manual testing or integration testing:

1. **UI interactions** — clicking, dragging, network editor, pane navigation
2. **Cross-session persistence** — requires closing/reopening the `.toe` file
3. **Keyboard shortcuts** — actual key press detection and OS integration
4. **Modal dialogs** — file pickers, user prompts, confirmation dialogs
5. **Undo/redo** — TouchDesigner's undo system behavior
6. **Graphics rendering** — visual output validation of TOPs
7. **Real-time performance** — sustained load, frame-rate stability
8. **External hardware** — MIDI, OSC, DMX, serial I/O
9. **Thread Manager under extreme load** — concurrent thread pool saturation (basic lifecycle is tested)

### Writing New Tests

Create a new test file in `dev/embody/unit_tests/`:

```python
"""Test suite: description of what this tests."""

# Base class is auto-injected by the test runner
class TestMyFeature(EmbodyTestCase):

    def test_something(self):
        """Test description."""
        # Create test fixtures in self.sandbox
        op = self.sandbox.create(baseCOMP, 'test_op')

        # Access Embody extension
        result = self.embody_ext.someMethod(op)

        # Assertions
        self.assertEqual(result, expected_value)
        self.assertTrue(op.valid)
        self.assertIn('foo', result)

    def setUp(self):
        """Called before each test (optional)."""
        pass

    def tearDown(self):
        """Called after each test (auto-destroys sandbox children)."""
        super().tearDown()  # Important: cleans up sandbox
```

**Key objects injected:**
- `self.sandbox` — baseCOMP for creating temporary operators
- `self.embody` — reference to `op.Embody`
- `self.embody_ext` — direct access to `op.Embody.ext.Embody`
- `self.runner` — TestRunnerExt instance
- All TD globals (`op`, `parent`, `root`, etc.) and operator types are available

**Verification strategy:**
1. **Unit tests** (automated) — test all business logic, MCP tools, and utilities
2. **Manual TD testing** — verify UI interactions, keyboard shortcuts, visual behavior
3. **MCP verification** — use Envoy tools to verify state (e.g., `get_externalizations`, `get_op_errors`)
4. **File inspection** — confirm externalized files in `dev/embody/` match expectations
5. **Log analysis** — after test runs, check `dev/logs/` for errors (see Important Rule #12)

## Common Mistakes to Avoid

1. Using `.val` instead of `.eval()` to read parameter values — `.val` only returns the constant-mode value
2. Referencing `op()` at module scope instead of inside functions/methods — causes recompilation cascades
3. Naming a DAT the same as a Python stdlib module (e.g., `json`, `os`) — shadows the real module
4. Using `op()` when `opex()` would catch missing operators immediately with a clear error
5. Accessing `op.TDResources.ThreadManager` from a worker thread — triggers THREAD CONFLICT
6. Relying on `op.id` for tracking — IDs change across sessions, copy/paste, and undo
7. Forgetting `extensionsReady` guards in parameter expressions that reference promoted attributes
8. Using `hasattr` for parameters known to exist in the `.toe`
9. Setting toggle parameters with `"True"`/`"False"` strings instead of `"0"`/`"1"`
10. Using backslashes in file paths instead of forward slashes
11. Changing MCP tool function signatures without considering API compatibility
12. Binding the MCP server to `0.0.0.0` instead of `127.0.0.1`
13. Editing `externalizations.tsv` directly instead of using Embody's tracking API
14. Importing or calling TouchDesigner modules in worker thread code (`EnvoyMCPServer` class)
15. Renaming externalized files on disk (`git mv`, manual rename) or manually updating `file`/`externaltox` parameters after a rename — Embody handles all of this automatically via `checkOpsForContinuity`. Only rename the operator itself (via MCP `rename_op` or inside TD)
16. Not following the `NameExt` convention for extension class names and their source DATs (e.g., `EmbodyExt`, `EnvoyExt`, `TestRunnerExt`)
17. Setting `.val` on a parameter in expression mode — silently switches to constant mode and destroys the expression
18. Using `print()` instead of `debug()` — loses source DAT name and line number context
19. Using `changeType()` without capturing the return value — the original operator reference becomes invalid. Always: `new_op = old_op.changeType(waveCHOP)`
20. Using `COMP.copy()` for multiple connected operators — connections between them are lost. Use `copyOPs([list])` to preserve inter-operator wiring
21. Calling `addError()`/`addWarning()` outside a cook callback — silently does nothing. Use `addScriptError()` from extension methods
22. Using `TOP.sample()` in loops or per-frame callbacks — downloads the entire texture from GPU every call. Use `numpyArray()` for batch access
23. Using `fetch()` without `search=False` when local-only lookup is intended — by default it searches up the parent hierarchy, which may return a parent's value instead
24. Calling `mod.moduleName.func()` in a loop without caching — re-resolves the DAT lookup every call. Cache: `m = mod.moduleName; m.func()`
25. Assigning directly to a `tdu.Dependency` object (`dep = 5`) instead of its value (`dep.val = 5`) — destroys the Dependency, silently breaking all dependent expressions
26. Caching extension references in local variables (`ext = self.ownerComp.ext.Embody`) — the reference goes stale when TD reinitializes the extension. Always call inline: `self.ownerComp.ext.Embody.Method()`
27. Creating operators without setting explicit positions — all new operators default to `[0, 0]` and stack on top of each other. After creating operators (via MCP `create_op`, `execute_python`, or `import_network`), ALWAYS use `set_op_position` to place them on the 200-unit grid with proper spacing (see Network Layout Conventions). Never rely on `layout()` for production networks — it produces unreadable layouts with no logical grouping. `layout()` is acceptable only as a fallback for temporary/throwaway networks.

## Important Rules

1. **ALWAYS use Envoy MCP tools to inspect and modify anything inside TouchDesigner** — Envoy gives you full access to ALL operators, parameters, widget properties, and network state inside the live TD session. NEVER say "I can't edit that because it's in a .tox" or "these are binary files I can't access." Use `get_op`, `set_parameter`, `get_parameter`, `execute_python`, `query_network`, `export_network`, and other MCP tools to read and modify anything. This is the entire point of the Envoy MCP server. If you need to change a widget color, read a parameter expression, or inspect a component's children — use the MCP tools. The filesystem is for externalized `.py` files; MCP is for everything else in the live TD environment.
2. **Do NOT assume network paths** — never guess `/project1`. Use `query_network` on `/` to discover the actual root structure before creating or referencing operators. Projects may have `/project1`, children directly under `/`, or custom names.
3. **Default to the current network** — when a user asks to create an operator without specifying a location, create it in the **current network**. Use `execute_python` with `result = ui.panes.current.owner.path` to determine the active network pane.
4. **Never edit `externalizations.tsv` directly** — it is managed exclusively by Embody's tracking system
5. **Always use forward slashes** in file paths for cross-platform compatibility
6. **Always consult the TD wiki** before writing or modifying TouchDesigner Python code — confirm API behavior even if you're confident
7. **Binary files** (`.toe`, `.tox`) cannot be read or diffed as files on disk — use MCP tools to inspect their contents in the live TD session, and work with externalized `.py` files for version-controlled code
8. **Thread boundary**: `EnvoyMCPServer` (worker thread) must never import or call TouchDesigner modules. All TD access goes through `_execute_in_td()` → main thread
9. **Safe deletion only**: Never delete files outside Embody's tracking. Use `safeDeleteFile()` / `isTrackedFile()`
10. **Always check for errors after creating operators** — call `get_op_errors` (with `recurse=true`) immediately after creating and connecting operators. Many TD operators require specific input types or parameter configurations to function. Fix all errors before considering the task complete.
11. **CLAUDE.md, text_claude.md, and text_help.py must ALWAYS be kept in sync.** The template at `dev/embody/Embody/text_claude.md` generates per-project CLAUDE.md files. The help text at `dev/embody/Embody/help/text_help.py` is displayed in the Embody UI. Any documentation changes (keyboard shortcuts, supported formats, features, workflow) must be applied to all three files.
12. **Favor annotations over OP comments** — when documenting operators or groups of operators in the network, always use `create_annotation` (annotate mode with a title bar) instead of setting the `comment` property on individual operators. Annotations are more visible, support rich text, and can visually group related operators. Reserve OP comments for brief inline notes only.
13. **Always analyze log files after MCP operations** — after running tests, bulk externalizations, or any multi-step MCP workflow, read the log file at `dev/logs/` to verify no errors occurred. The piggybacked `_logs` field and `get_logs` ring buffer only hold a limited window — errors from earlier in the operation may have been evicted. Grep the log file for `ERROR` and `WARNING` entries and resolve any issues before reporting success.
14. **Always update unit tests when modifying project code.** When changing any extension file (EmbodyExt.py, EnvoyExt.py, TDNExt.py, etc.), check whether existing unit tests assert against the changed behavior — if so, update those assertions to match the new code. Never leave tests asserting against a format or API that no longer exists. Run the relevant test suite after changes to confirm all tests pass.
