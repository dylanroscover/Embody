# Embody + Envoy

## Project Overview

**Embody** is a TouchDesigner extension that automates externalization of COMP and DAT operators to version-control-friendly files (.tox, .py, .json, .xml, etc.). It solves the problem of TouchDesigner's binary .toe files being impossible to diff/merge in git.

**Envoy** is an MCP (Model Context Protocol) server embedded inside Embody that lets Claude Code create, modify, connect, and query TouchDesigner operators programmatically — plus manage Embody externalizations.

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

# Menu parameters accept both string name and index:
op('geo1').par.xord = 'trs'   # by name
op('geo1').par.xord = 5       # by index

# Type casting — direct method calls require explicit .eval():
me.par.tx.eval().hex()  # CORRECT
me.par.tx.hex()         # WRONG — parameter objects don't have .hex()
```

### `op()` vs `opex()`

`op()` returns `None` silently when an operator is not found. `opex()` raises an exception immediately with a clear error message. Prefer `opex()` when the operator must exist — it prevents hard-to-debug downstream `NoneType` errors.

```python
# op() returns None if not found — silent failure:
node = op('/nonexistent/path')
node.par.tx = 5  # AttributeError: 'NoneType' has no attribute 'par'

# opex() raises an exception immediately:
node = opex('/nonexistent/path')  # Raises tdError with clear message
```

Use `op()` only when `None` is an acceptable/expected result (e.g., checking if an operator exists).

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

### `extensionsReady` Guard Pattern

Parameter expressions that reference extension-promoted attributes must guard against initialization timing:

```python
# In a parameter expression:
parent().MyExtensionProperty if parent().extensionsReady else 0
```

Without this, TD raises "Cannot use an extension during its initialization" during the compile phase. For post-init logic that depends on other extensions or the network being fully cooked, use `onInitTD(self)`.

### Explicit Type Conversion

TD parameters and CHOP channels auto-cast in expression contexts but remain TD objects internally. When passing values to standard Python functions, explicitly convert with `int()`, `float()`, or `str()` to avoid type-mismatch bugs. Use `repr()` to reveal the actual type if uncertain.

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
├── docs/
│   └── TDN.md                            # TDN network format documentation
├── dev/
│   ├── Embody-5.61.toe                    # Active development project
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
│               └── text_help.py           # Help text
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
- **7-phase import**: Operators are created first, then custom parameters, parameter values, flags, connections, DAT content, and positions — in that specific order to satisfy dependencies
- **Relative source references**: Connections reference siblings by name only, falling back to full paths for cross-network references
- **Palette clone detection**: COMPs cloned from `/sys/` are marked but their children are not exported (TD recreates them automatically)
- **Per-COMP split mode**: Large networks can be exported as one `.tdn` file per COMP, creating a git-friendly directory structure

**File format**: JSON with `.tdn` extension. Full specification: [`docs/TDN.md`](docs/TDN.md)

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
- **Renaming operators**: To rename a TD operator, ONLY rename the operator itself — via MCP `rename_operator` or inside TouchDesigner. **NEVER** rename the externalized file on disk, **NEVER** manually update the `file`/`externaltox` parameter, and **NEVER** edit the externalizations table. Embody's `checkOpsForContinuity` handles everything automatically: it detects the stale path in the table, `_findMovedOp` matches the renamed operator by its file parameter, then `updateMovedOp` renames the file on disk, updates the `file`/`externaltox` parameter, and updates the table row — all in one step.
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
  - [`docs/TDN.md`](docs/TDN.md) — TDN network format specification (JSON schema for TD network export/import)

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

**Keyboard shortcut**: `Ctrl+Shift+E` exports the current network to a `.tdn` file.

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

Embody has a comprehensive automated test suite with **27 test files** covering all core functionality. The test framework lives at `/embody/unit_tests` and uses a custom test runner extension.

### Test Coverage

**Core Embody (13 suites):**
- `test_externalization.py` — externalization lifecycle
- `test_crud_operators.py` — create, read, update, delete operations
- `test_file_management.py` — file I/O, path handling, cleanup
- `test_tag_management.py` — tagging operators for externalization
- `test_tag_lifecycle.py` — tag application and removal
- `test_rename_move_lifecycle.py` — rename and move tracking
- `test_delete_cleanup.py` — deletion and file cleanup
- `test_duplicate_handling.py` — duplicate operator handling
- `test_update_sync.py` — sync between .toe and externalized files
- `test_path_utils.py` — path normalization and utilities
- `test_param_tracker.py` — parameter change tracking
- `test_operator_queries.py` — operator discovery and queries
- `test_logging.py` — logging system

**MCP Tools (11 suites):**
- `test_mcp_operators.py` — create, delete, copy, rename, query, find
- `test_mcp_parameters.py` — get/set parameters, modes, expressions
- `test_mcp_dat_content.py` — DAT text and table operations
- `test_mcp_connections.py` — wiring operators together
- `test_mcp_annotations.py` — creating and managing annotations
- `test_mcp_extensions.py` — extension creation and setup
- `test_mcp_diagnostics.py` — error checking, performance, info
- `test_mcp_flags_position.py` — operator flags and positioning
- `test_mcp_code_execution.py` — executing Python in TD
- `test_mcp_externalization.py` — Embody integration via MCP
- `test_mcp_performance.py` — performance monitoring

**TDN Format (2 suites):**
- `test_tdn_export_import.py` — network export/import
- `test_tdn_helpers.py` — TDN utility functions

**Infrastructure (1 suite):**
- `test_server_lifecycle.py` — Envoy MCP server start/stop

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

## Important Rules

1. **Do NOT assume network paths** — never guess `/project1`. Use `query_network` on `/` to discover the actual root structure before creating or referencing operators. Projects may have `/project1`, children directly under `/`, or custom names.
2. **Default to the current network** — when a user asks to create an operator without specifying a location, create it in the **current network**. Use `execute_python` with `result = ui.panes.current.owner.path` to determine the active network pane.
3. **Never edit `externalizations.tsv` directly** — it is managed exclusively by Embody's tracking system
4. **Always use forward slashes** in file paths for cross-platform compatibility
5. **Always consult the TD wiki** before writing or modifying TouchDesigner Python code — confirm API behavior even if you're confident
6. **Binary files** (`.toe`, `.tox`) cannot be read or diffed — work with the externalized `.py` files instead
7. **Thread boundary**: `EnvoyMCPServer` (worker thread) must never import or call TouchDesigner modules. All TD access goes through `_execute_in_td()` → main thread
8. **Safe deletion only**: Never delete files outside Embody's tracking. Use `safeDeleteFile()` / `isTrackedFile()`
9. **Always check for errors after creating operators** — call `get_op_errors` (with `recurse=true`) immediately after creating and connecting operators. Many TD operators require specific input types or parameter configurations to function. Fix all errors before considering the task complete.
10. **CLAUDE.md, text_claude.md, and text_help.py must ALWAYS be kept in sync.** The template at `dev/embody/Embody/text_claude.md` generates per-project CLAUDE.md files. The help text at `dev/embody/Embody/help/text_help.py` is displayed in the Embody UI. Any documentation changes (keyboard shortcuts, supported formats, features, workflow) must be applied to all three files.
11. **Favor annotations over OP comments** — when documenting operators or groups of operators in the network, always use `create_annotation` (annotate mode with a title bar) instead of setting the `comment` property on individual operators. Annotations are more visible, support rich text, and can visually group related operators. Reserve OP comments for brief inline notes only.
12. **Always analyze log files after MCP operations** — after running tests, bulk externalizations, or any multi-step MCP workflow, read the log file at `dev/logs/` to verify no errors occurred. The piggybacked `_logs` field and `get_logs` ring buffer only hold a limited window — errors from earlier in the operation may have been evicted. Grep the log file for `ERROR` and `WARNING` entries and resolve any issues before reporting success.
