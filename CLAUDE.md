# Embody + Claudius

## Project Overview

**Embody** is a TouchDesigner extension that automates externalization of COMP and DAT operators to version-control-friendly files (.tox, .py, .json, .xml, etc.). It solves the problem of TouchDesigner's binary .toe files being impossible to diff/merge in git.

**Claudius** is an MCP (Model Context Protocol) server embedded inside Embody that lets Claude Code create, modify, connect, and query TouchDesigner operators programmatically — plus manage Embody externalizations.

Current version: **v5.0.32** for TouchDesigner 2025.32050.

## TouchDesigner Development

- TouchDesigner **auto-reinitializes extensions** when their source DATs change (including externalized `.py` files synced from disk). However, old extension instances may linger in memory due to Python garbage collection issues (circular references, cached callbacks, etc.). To ensure clean teardown, implement `onDestroyTD(self)` in your extension class — TD calls it on the old instance before reinitializing. For post-init setup that needs a fully-cooked network, use `onInitTD(self)` (called at end of the frame the extension initialized). See: https://docs.derivative.ca/Extensions#Gotcha:_extensions_staying_in_memory_-_Solution:_onDestroyTD
- When working with TouchDesigner parameters, prefer `par.name` for parameter identification.
- **Toggle parameters** use `0`/`1` (not `"True"`/`"False"`). When setting a toggle via `set_parameter`, pass `value="0"` or `value="1"`.

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
│   ├── Embody-5.31.toe                    # Active development project
│   ├── requirements.txt                   # MCP dependencies (mcp, pywin32)
│   ├── TDPyEnvManagerContext.json         # Python 3.11 vEnv config
│   ├── dev_vEnv/                          # Python virtual environment
│   ├── Backup/                            # Versioned .toe backups
│   └── embody/
│       ├── externalizations.tsv           # Externalization tracking table (managed by Embody)
│       └── Embody/                        # Main extension source
│           ├── EmbodyExt.py               # Core externalization engine (~2,200 lines)
│           ├── ClaudiusExt.py             # MCP server extension (~2,500 lines)
│           ├── TDNExt.py                  # TDN network format export/import (~1,500 lines)
│           ├── CLAUDE_md_template.md      # Template for generating per-project CLAUDE.md
│           ├── execute.py                 # Project lifecycle callbacks
│           ├── parexec.py                 # Parameter change callbacks
│           ├── keyboardin_callbacks.py    # Keyboard shortcut handlers
│           ├── timer_callbacks.py         # Timer callbacks (double-press detection)
│           ├── chopexec_exit_tagger.py    # CHOP exit handler
│           └── help/
│               └── text_help.py           # Help text
└── release/
    └── Embody-v5.0.32.tox                # Latest release build
```

## Architecture

### Externalization Sync (.toe ↔ .py files)

TouchDesigner projects are binary `.toe` files. Embody externalizes tagged operators to text files under `dev/embody/`:

1. **Tagging**: Operators are tagged for externalization (keyboard shortcut or MCP tool)
2. **Sync out**: On `Ctrl+Shift+U` or project save, Embody writes tagged operators to files (`.tox` for COMPs, `.py`/`.json`/etc. for DATs)
3. **Sync in**: When the `.toe` is opened, TouchDesigner reads the external files back into operators via their `file` parameter
4. **Tracking**: `dev/embody/externalizations.tsv` tracks all externalized ops with path, type, timestamp, dirty state, and build number

**Important**: Edits to `.py` files in `dev/embody/Embody/` are read by TD when the project loads or the DAT syncs. Changes made inside TD are written out to these files on save. This bidirectional sync is the core of the system.

### Claudius MCP Architecture

Claudius uses a dual-thread design:

- **Worker thread**: Runs the MCP server (FastMCP with Streamable HTTP transport via uvicorn) — no TouchDesigner imports allowed
- **Main thread**: Executes all TD operations via `ClaudiusExt._onRefresh()` callback
- **Communication**: `threading.Event` + `Queue` for request/response between threads
- **Thread management**: Uses `op.TDResources.ThreadManager` (TDTask pattern)
- **Graceful shutdown**: `shutdown_event` (threading.Event) signals uvicorn to exit cleanly
- **Version**: `CLAUDIUS_VERSION` constant tracks server version for compatibility

## Architecture Notes

- **Stateless HTTP transport**: Claudius uses `stateless_http=True` because TD's single-threaded model means concurrent sessions would queue on the same main-thread execution path anyway. Stateless mode simplifies the implementation and avoids session management overhead.
- **30-second operation timeout**: `_execute_in_td()` times out at 30 seconds. This prevents indefinite hangs if the main thread is blocked (e.g., modal dialog), while allowing enough time for heavy operations like `.tox` saves. If a TD operation takes longer, the MCP tool returns a timeout error — the operation may need to be broken into smaller steps.
- **127.0.0.1 binding**: Security requirement to prevent DNS rebinding attacks. Claudius must never bind to `0.0.0.0` or be accessible from the network.
- **Standalone thread**: The MCP server runs as a `standalone=True` TDTask because it is long-lived (runs for the entire session), unlike pool tasks which are meant for short-lived work units.
- **Queue-based cross-thread communication**: Uses `threading.Event` + `Queue` rather than locks because TD's cook cycle is frame-based — the main thread can only process requests once per frame via the RefreshHook.

### Graceful Shutdown Sequence

1. `ClaudiusExt.Stop()` is called (from UI toggle, `onExit`, or project close)
2. `shutdown_event.set()` signals the worker thread's uvicorn server to stop
3. Uvicorn completes its shutdown (stops accepting connections, drains existing)
4. Worker thread's target function returns
5. `SuccessHook` or `ExceptHook` fires on the main thread for cleanup
6. Port is released and available for rebinding

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

# Claudius-specific extensions
op.Embody.ext.Claudius.Start()
op.Embody.ext.Claudius.Stop()
```

## Code Conventions

- **Logging**: Use `op.Embody.Log(message, level)` from anywhere in the project. Levels: `'DEBUG'`, `'INFO'`, `'WARNING'`, `'ERROR'`, `'SUCCESS'`. Convenience methods: `op.Embody.Debug(msg)`, `op.Embody.Info(msg)`, `op.Embody.Warn(msg)`, `op.Embody.Error(msg)`. Logs go to: FIFO DAT (TD UI), textport (if `Print` par enabled), file (if `Logtofile` par enabled), and ring buffer (MCP access via `get_logs` tool + auto-piggybacked on all MCP tool responses in the `_logs` field)
- **Paths**: Always use forward slashes (`/`) for cross-platform compatibility — never backslashes
- **File safety**: Only delete files tracked by Embody (`isTrackedFile()`, `safeDeleteFile()`). Never delete untracked files
- **Directory cleanup**: Use `rmdir()` only (fails on non-empty dirs) — never `shutil.rmtree()`
- **Error handling**: Wrap TD operations in try/except, return `{'error': str(e)}` dicts in MCP handlers
- **Thread safety**: Never import or call TouchDesigner modules in worker thread code (`ClaudiusMCPServer` class)
- **Table management**: The `externalizations.tsv` is managed exclusively by Embody — never edit it directly
- **No `hasattr` for known parameters**: Embody's custom parameters (e.g., `Claudiusenable`, `Claudiusport`, `Claudiusstatus`) are static and locked in the `.toe` — they always exist. Do not wrap access in `hasattr(self.ownerComp.par, ...)` checks. Just use them directly (e.g., `self.ownerComp.par.Claudiusstatus = 'Running'`)
- **MCP error types**: Claudius handles two error categories: (1) Protocol errors (JSON-RPC level) for unknown tools, invalid arguments, or server errors — FastMCP handles these automatically. (2) Tool execution errors returned in tool results via `{'error': str(e)}` dicts — these indicate the tool ran but encountered a problem (missing operator, invalid path, etc.). Always return structured error information rather than raising exceptions in tool handlers.
- **MCP input validation**: All tool handlers must validate inputs before passing to TD operations. Check that `op_path` is a valid path format, verify operators exist before operating on them, validate parameter names, and sanitize string inputs passed to `eval()` or `exec()`.
- **Localhost binding**: Claudius must bind to `127.0.0.1`, never `0.0.0.0`. Binding to all interfaces would expose the MCP server to the local network and enable DNS rebinding attacks from malicious websites.
- **Tool signatures are MCP schema**: FastMCP generates tool definitions from function signatures and docstrings in `_register_tools()`. Changing parameter names, type hints, or docstrings changes the tool's public MCP interface. Treat these as API contracts — changes may break client integrations.

## File Editing Impact

| File | Impact | Notes |
|------|--------|-------|
| `EmbodyExt.py` | HIGH | Core engine. Changes affect all externalization behavior. |
| `ClaudiusExt.py` | HIGH | MCP server. Two distinct sections: `ClaudiusMCPServer` (worker thread, no TD imports) and `ClaudiusExt` (main thread, TD access). Tool signature changes break client API. |
| `TDNExt.py` | MEDIUM | Network export/import. Changes affect `.tdn` format compatibility. |
| `execute.py` | LOW | Project lifecycle callbacks (`onStart`, `onProjectPreSave`, etc.). Rarely needs changes. |
| `parexec.py` | MEDIUM | Fires on every parameter change. Performance-sensitive. |
| `keyboardin_callbacks.py` | LOW | Keyboard shortcut handlers. Additive changes are safe. |
| `timer_callbacks.py` | LOW | Double-press detection logic. |
| `chopexec_exit_tagger.py` | LOW | CHOP exit handler for tagging. |
| `externalizations.tsv` | NEVER EDIT | Managed exclusively by Embody. Manual edits corrupt tracking. |
| `CLAUDE_md_template.md` | MEDIUM | Template for per-project CLAUDE.md. Must be kept in sync with root CLAUDE.md. |

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
  - https://docs.derivative.ca/Extensions — Extensions system (lifecycle, promotion, `onDestroyTD`, `onInitTD`, `StorageManager`)

## Claudius MCP Server Setup

### Prerequisites
1. Python 3.11 vEnv configured via TDPyEnvManager (see `dev/TDPyEnvManagerContext.json`)
2. Install dependencies: `pip install -r dev/requirements.txt` (mcp>=1.2.0, pywin32>=306)

### Enabling the Server
1. Open the Embody `.toe` project in TouchDesigner
2. Toggle the `Claudiusenable` parameter ON in the Embody COMP
3. Server starts on configured port (default: 9876)

### Connecting Claude Code
Claudius auto-creates a `.mcp.json` file in the git repo root on startup. This works with both the Claude Code CLI and the VS Code extension. Just start a new Claude Code session after Claudius is running.

If you need to configure manually, create `.mcp.json` in the project root:
```json
{
  "mcpServers": {
    "claudius": {
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
| `create_operator` | `parent_path`, `op_type`, `name?` | Create a new operator (e.g., `baseCOMP`, `noiseTOP`, `textDAT`) |
| `create_extension` | `parent_path`, `class_name`, `name?`, `code?`, `promote?`, `ext_name?`, `ext_index?`, `existing_comp?` | Create a TD extension: baseCOMP + text DAT + extension wiring, initialized and ready to use |
| `delete_operator` | `op_path` | Delete an operator |
| `copy_operator` | `source_path`, `dest_parent`, `new_name?` | Copy operator to new location |
| `rename_operator` | `op_path`, `new_name` | Rename an operator |
| `get_operator` | `op_path` | Get full operator info (type, family, parameters, inputs, outputs, children) |
| `query_network` | `parent_path?`, `recursive?`, `op_type?`, `include_utility?` | List operators in a container. Set `include_utility=True` to include annotations |
| `find_children` | `op_path`, `name?`, `type?`, `depth?`, `tags?`, `text?`, `comment?`, `include_utility?` | Advanced search using TD's `findChildren` — filter by name pattern, type, depth, tags, text content, or comment. Set `include_utility=True` to include annotations |
| `cook_operator` | `op_path`, `force?`, `recurse?` | Force-cook an operator |

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

### Node Positioning & Layout

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_position` | `op_path` | Get node position, size, color, and comment |
| `set_op_position` | `op_path`, `x?`, `y?`, `width?`, `height?`, `color?`, `comment?` | Set node position, size, color (`[r,g,b]` floats 0-1), or comment |
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
| `connect_operators` | `source_path`, `dest_path`, `source_index?`, `dest_index?`, `comp?` | Wire two operators together. Set `comp=True` for COMP connectors (top/bottom) |
| `disconnect_operator` | `op_path`, `input_index?`, `comp?` | Disconnect an operator's input. Set `comp=True` for COMP connectors (top/bottom) |
| `get_connections` | `op_path` | Get all input/output connections (includes COMP connections for COMPs) |

### Code Execution

| Tool | Parameters | Description |
|------|-----------|-------------|
| `execute_python` | `code` | Execute Python code in TD. Set `result` variable to return values |

### Introspection & Diagnostics

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_td_info` | _(none)_ | Get TD version, build, OS, and Claudius version |
| `get_node_errors` | `op_path`, `recurse?` | Get error messages for an operator and its children |
| `exec_node_method` | `op_path`, `method`, `args?`, `kwargs?` | Call a method on an operator (e.g., `appendRow`, `cook`) |
| `get_td_classes` | _(none)_ | List all Python classes/modules in the `td` module |
| `get_td_class_details` | `class_name` | Get methods, properties, and docs for a TD class |
| `get_module_help` | `module_name` | Get Python help text for a module (supports dotted names like `td.tdu`) |

### MCP Prompts

| Prompt | Parameters | Description |
|--------|-----------|-------------|
| `search_node` | `node_name`, `node_type?` | Guide for searching nodes by name |
| `check_node_errors` | `node_path` | Guide for inspecting and resolving node errors |
| `connect_nodes` | _(none)_ | Guide for wiring nodes together |
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

## Common Workflows

### Creating an Operator and Verifying It
1. `query_network` on the target parent to confirm it exists
2. `create_operator` with the desired type and name
3. `get_node_errors` with `recurse=true` to check for errors
4. If connecting: `connect_operators` then `get_node_errors` again

### Adding a New MCP Tool to Claudius
1. Add the tool function inside `_register_tools()` in `ClaudiusExt.py`
2. Add a corresponding handler case in `_onRefresh()` for the TD operation
3. Update the MCP Tool Reference table in `CLAUDE.md`
4. Update `CLAUDE_md_template.md` to match
5. Test via MCP Inspector or Claude Code

### Debugging a Node Error
1. `get_node_errors` with `recurse=true` on the suspected node
2. `get_operator` to inspect parameters and connections
3. `get_connections` to verify input/output wiring
4. `get_dat_content` if the node is a DAT with script errors

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

There is no automated test suite. Changes are verified by:

1. **Manual TD testing**: Open the `.toe` in TouchDesigner and exercise the feature (tag operators, externalize, rename, etc.)
2. **MCP-based verification**: Use Claudius MCP tools to verify state — e.g., call `get_externalizations` to confirm an operator was tracked, or `get_operator` to check parameter values
3. **File inspection**: Check that externalized files in `dev/embody/` reflect expected content

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
14. Importing or calling TouchDesigner modules in worker thread code (`ClaudiusMCPServer` class)

## Important Rules

1. **Do NOT assume network paths** — never guess `/project1`. Use `query_network` on `/` to discover the actual root structure before creating or referencing operators. Projects may have `/project1`, children directly under `/`, or custom names.
2. **Default to the current network** — when a user asks to create an operator without specifying a location, create it in the **current network**. Use `execute_python` with `result = ui.panes.current.owner.path` to determine the active network pane.
3. **Never edit `externalizations.tsv` directly** — it is managed exclusively by Embody's tracking system
4. **Always use forward slashes** in file paths for cross-platform compatibility
5. **Always consult the TD wiki** before writing or modifying TouchDesigner Python code — confirm API behavior even if you're confident
6. **Binary files** (`.toe`, `.tox`) cannot be read or diffed — work with the externalized `.py` files instead
7. **Thread boundary**: `ClaudiusMCPServer` (worker thread) must never import or call TouchDesigner modules. All TD access goes through `_execute_in_td()` → main thread
8. **Safe deletion only**: Never delete files outside Embody's tracking. Use `safeDeleteFile()` / `isTrackedFile()`
9. **Always check for errors after creating operators** — call `get_node_errors` (with `recurse=true`) immediately after creating and connecting operators. Many TD operators require specific input types or parameter configurations to function. Fix all errors before considering the task complete.
10. **CLAUDE.md and CLAUDE_md_template.md must ALWAYS be kept in sync.** The template at `dev/embody/Embody/CLAUDE_md_template.md` generates per-project CLAUDE.md files. Any documentation changes must be applied to both files.
11. **Favor annotations over OP comments** — when documenting operators or groups of operators in the network, always use `create_annotation` (annotate mode with a title bar) instead of setting the `comment` property on individual operators. Annotations are more visible, support rich text, and can visually group related operators. Reserve OP comments for brief inline notes only.
