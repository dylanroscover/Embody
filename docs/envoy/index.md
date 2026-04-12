# Envoy MCP Server

**Envoy** is the forward velocity layer of the project. It's an MCP (Model Context Protocol) server embedded inside your `.toe` file — which means your AI assistant connects directly to your live TouchDesigner session. Not a description of your network. Not a snapshot from last time you saved. The live session: operators, parameters, connections, cook state, and pixel output, all accessible in real time. You describe what you want, and the AI builds it, wires it, tunes it, and captures what it looks like.

## Why Envoy?

TouchDesigner has no external API. A `.toe` file has no access surface — nothing outside TD can read it, write to it, or interact with what's running inside it. AI assistants hitting this wall have two options: describe what a network *might* look like and hope you can implement it, or stop. Neither is useful when you're mid-session with a half-built network in front of you.

Envoy exists to change that. It runs an HTTP server embedded in your `.toe` as a COMP extension, exposes 46 MCP tools that map to live TD operations, and auto-configures your AI client to connect to it on startup. The moment Envoy starts, your AI assistant gains full access to everything running in your session.

## Key Design Principles

### Dual-Thread Safety

TD's Python runtime is single-threaded — all TD objects must be accessed from the main thread. Envoy's HTTP server runs on a background worker thread to remain non-blocking. When a tool call arrives, the worker thread enqueues the operation and waits. The main thread picks it up during `_onRefresh()`, executes it with full TD access, and signals the result back. **No TD object is ever touched off the main thread.**

### Embedded, Not External

Envoy lives inside your `.toe` as a COMP extension. It starts with your project, stops when you stop it, and automatically restarts on port change or crash (exponential backoff, up to 3 attempts). There's no sidecar process to manage, no daemon to install, no separate server to launch. Enable it once in your Embody settings and it runs with your project from that point on.

### Coarse, Composable Tools

Each tool is designed to do a meaningful unit of work, not one atomic operation. `get_network_layout` returns positions for every operator in a COMP in a single call — not N calls to `get_op_position`. `batch_operations` combines any set of tool calls into one round-trip. `execute_python` lets the AI run arbitrary Python when a task needs looping, branching, or computed values between steps. The design principle: minimize round-trips, maximize work per call.

### Localhost-Only Binding

The server binds to `127.0.0.1` only and is not reachable from the network. The MCP bridge script that connects your AI client to Envoy also runs locally. Your TD session is not exposed to external connections.

### Piggybacked Diagnostics

Every MCP tool response includes a `_logs` field with up to 20 log entries generated since the previous call. The AI gets a running stream of what's happening inside TD — cook errors, warnings, extension messages — without polling separately. No context is lost between tool calls.

## How Envoy Works

A tool call travels this path:

1. **AI client** sends a tool call via the MCP protocol (STDIO)
2. **Bridge script** (`.embody/envoy-bridge.py`) receives it and forwards to Envoy's HTTP server on localhost
3. **Envoy** (worker thread) validates the request, enqueues the TD operation, and waits for the result
4. **TD main thread** picks up the operation during `_onRefresh()`, executes it with full TD access, and posts the result
5. **Envoy** returns the response to the bridge with recent log entries piggybacked
6. **Bridge** returns the MCP response to the AI client

The bridge handles the MCP protocol handshake locally and keeps bridge meta-tools (`get_td_status`, `launch_td`, etc.) available even when TD is not running.

## Capabilities

Envoy exposes **46 MCP tools** across 16 categories.

### Operator Management

Build, query, copy, rename, and delete operators. `create_op` is how any network comes to be — pass a TD operator type string (`noiseTOP`, `baseCOMP`, `textDAT`, etc.) and it appears. `query_network` lists everything in a container; `find_children` does deep filtered search by name pattern, type, depth, tags, or text content. The AI reads the existing network before touching anything.

| Tool | Description |
|---|---|
| `create_op` | Create a new operator by type string |
| `create_extension` | Create a TD extension: baseCOMP + text DAT + wiring, initialized and ready |
| `delete_op` | Delete an operator |
| `copy_op` | Copy an operator to a new location |
| `rename_op` | Rename an operator |
| `get_op` | Get full operator info: type, family, parameters, inputs, outputs, children |
| `query_network` | List operators in a container, with optional annotation inclusion |
| `find_children` | Deep search by name, type, depth, tags, text content, or comment |
| `cook_op` | Force-cook an operator |

### Parameter Control

`get_parameter` and `set_parameter` handle everything: constant values, expressions, bind expressions, export connections, and mode switches. `get_parameter` returns the full context — label, range, menu entries, current expression, default. `set_parameter` writes any mode in a single call.

| Tool | Description |
|---|---|
| `get_parameter` | Read value, mode, expression, bind info, label, range, menu entries, and default |
| `set_parameter` | Write value, expression, bind expression, or switch mode (`constant`/`expression`/`export`/`bind`) |

### DAT Content

Read and write text DATs (Python scripts, configs, console output) and table DATs (structured data). The AI can edit extension code in place, update lookup tables, and read diagnostic output without leaving the session.

| Tool | Description |
|---|---|
| `get_dat_content` | Get DAT text or table data |
| `set_dat_content` | Write text string or structured table rows |

### Operator Flags

Toggle bypass, lock, display, render, viewer, expose, and cook permissions — in bulk, on any operator. Useful for isolating a broken section of a network, preventing accidental edits to finished components, or temporarily disabling expensive operators to improve performance.

| Tool | Description |
|---|---|
| `get_op_flags` | Read all flags: bypass, lock, display, render, viewer, current, expose, allowCooking |
| `set_op_flags` | Set one or more flags in a single call |

### Positioning & Layout

`get_network_layout` returns every operator's position, size, and color in a COMP in one call, along with the bounding box. The AI uses this before placing anything, so new operators extend existing rows rather than overlap them. `set_op_position` positions individual operators with color and comment support.

| Tool | Description |
|---|---|
| `get_network_layout` | Get positions of all operators (and annotations) in a COMP in one call |
| `get_op_position` | Get position, size, color, and comment for one operator |
| `set_op_position` | Set position, size, color, or comment |
| `layout_children` | Auto-layout all children in a COMP |

### Annotations

Create, read, and modify network boxes and comment annotations. The AI can group and label operators the same way a human would — drawing a box around a signal chain, titling it, and keeping it clean as the network grows.

| Tool | Description |
|---|---|
| `create_annotation` | Create an annotation (`annotate`, `comment`, or `networkbox` mode) |
| `get_annotations` | List all annotations in a COMP with their properties and enclosed operators |
| `set_annotation` | Modify text, title, color, opacity, position, or size |
| `get_enclosed_ops` | Get operators enclosed by an annotation, or annotations enclosing an operator |

### Connections

Wire operators together and inspect the full connection graph. Works for all standard operator families and for COMP parent/child connectors.

| Tool | Description |
|---|---|
| `connect_ops` | Wire two operators together (specify source/dest index, or use `comp=True` for COMP connectors) |
| `disconnect_op` | Disconnect an operator's input |
| `get_connections` | Get all input/output connections, including COMP connections |

### Performance Monitoring

`get_op_performance` reads CPU and GPU cook times, memory usage, and cook counts for any operator. `get_project_performance` returns project-level FPS, frame time, dropped frames, active ops, GPU temperature, and an optional ranked list of the top N most expensive COMPs. The AI can identify bottlenecks without you needing to point at them.

| Tool | Description |
|---|---|
| `get_op_performance` | CPU/GPU cook times, memory, cook counts for an operator |
| `get_project_performance` | Project FPS, frame time, memory, dropped frames, GPU temp, hotspot ranking |

### Code Execution

`execute_python` runs arbitrary Python in TD's main thread and returns whatever you assign to the `result` variable. This is the escape hatch for everything that doesn't map cleanly to a dedicated tool: iterating a list of operators, computing positions before placing, reading live storage, calling extension methods directly, or running any Python operation that needs full TD access.

| Tool | Description |
|---|---|
| `execute_python` | Execute Python in TD's main thread; return values via `result` variable |

### Introspection & Diagnostics

Inspect the live TD Python API, check operator errors, and call operator methods by name. The AI can look up an unfamiliar API class while building code that uses it, without leaving the session.

| Tool | Description |
|---|---|
| `get_td_info` | TD version, build, OS, and Envoy version |
| `get_op_errors` | Error and warning messages for an operator and its children |
| `exec_op_method` | Call a method on an operator by name (e.g., `appendRow`, `cook`) |
| `get_td_classes` | List all Python classes/modules in the `td` module |
| `get_td_class_details` | Methods, properties, and docs for a TD class |
| `get_module_help` | Python help text for a module (supports dotted names like `td.tdu`) |

### Embody Integration

Tag operators for externalization, query status, and force-save to disk — the full Embody lifecycle as MCP tools. The AI can externalize a COMP it just finished building and confirm the file landed on disk, without you switching focus.

| Tool | Description |
|---|---|
| `externalize_op` | Tag and externalize an operator (auto-detects type) |
| `remove_externalization_tag` | Remove an externalization tag |
| `get_externalizations` | List all externalized operators with status |
| `save_externalization` | Force-save an externalized operator to disk |
| `get_externalization_status` | Dirty state, build number, timestamp, file path |

### TDN Format

Export any COMP to `.tdn` JSON and import JSON back as a live network. Used for Embody's TDN externalization strategy and for taking readable, diffable snapshots of network state mid-session.

| Tool | Description |
|---|---|
| `export_network` | Export a network to `.tdn` JSON (non-default properties only) |
| `import_network` | Recreate a network from `.tdn` JSON |

### TOP Capture

`capture_top` downloads a TOP's current frame output as an image and returns it directly in the MCP response as an `ImageContent` attachment — meaning the AI actually *sees* the pixel output, not a description of it. This closes the visual feedback loop: the AI builds a compositing chain, captures the output, examines what's rendering, and iterates — without you describing the result in words.

Small images (under 20 KB) are returned inline. Larger images are saved to a temp file and the path is returned. JPEG (default, 80% quality) and PNG are supported, with configurable maximum resolution (default: 640px long edge).

| Tool | Description |
|---|---|
| `capture_top` | Capture a TOP's current output as an image; returns inline preview for small images |

### Logging

`get_logs` reads the ring buffer (up to 200 entries) with incremental polling via `since_id` — request only entries you haven't seen yet. Every tool response also piggybacks up to 20 recent entries automatically, so the AI is always looking at current state.

| Tool | Description |
|---|---|
| `get_logs` | Read log entries with level, source, and incremental-polling filters |
| `run_tests` | Run test suites and return results |

### Bridge Meta-Tools

These tools run on the local bridge process, not inside TD. They're available even when TD is not running — this is how the AI can launch TD from scratch, detect crashes, and recover connectivity without any input from you.

| Tool | Description |
|---|---|
| `get_td_status` | Connection state, process liveness, crash detection, restart attempts, instance registry |
| `launch_td` | Launch TD with the project's `.toe` file; waits for Envoy to become reachable |
| `restart_td` | Gracefully quit and relaunch TD |
| `switch_instance` | List registered TD instances or switch to a different running instance |

### Batch Operations

`batch_operations` combines multiple tool calls into a single HTTP round-trip, eliminating per-call latency and token overhead. Any set of tools can be batched. When you need conditionals, loops, or values computed between steps, use `execute_python` instead.

| Tool | Description |
|---|---|
| `batch_operations` | Execute a list of `{tool, params}` operations in one request; stops on first error |

**Example** — position 4 operators and wire them in one call:
```json
{"operations": [
  {"tool": "set_op_position", "params": {"op_path": "/project1/noise1", "x": 400, "y": 0}},
  {"tool": "set_op_position", "params": {"op_path": "/project1/level1", "x": 800, "y": 0}},
  {"tool": "set_op_position", "params": {"op_path": "/project1/comp1", "x": 1200, "y": 0}},
  {"tool": "set_op_position", "params": {"op_path": "/project1/null1", "x": 1600, "y": 0}},
  {"tool": "connect_ops", "params": {"source_path": "/project1/noise1", "dest_path": "/project1/level1"}},
  {"tool": "connect_ops", "params": {"source_path": "/project1/level1", "dest_path": "/project1/comp1"}},
  {"tool": "connect_ops", "params": {"source_path": "/project1/comp1", "dest_path": "/project1/null1"}}
]}
```

## Auto-Configuration

When Envoy starts for the first time, it generates a complete AI client configuration in your git repo root (or project folder if no git):

| File | Purpose |
|---|---|
| `.mcp.json` | Registers Envoy's MCP bridge with Claude Code and other MCP clients |
| `.claude/CLAUDE.md` | Project context for Claude Code — what Embody is, how the network is structured, what tools to use |
| `.claude/rules/` | Always-loaded coding conventions — TD Python patterns, parameter rules, network layout, MCP safety |
| `.claude/skills/` | On-demand reference — full MCP tool catalog, TD API reference, operator creation workflow, and more |
| `.claude/commands/` | Slash commands — `/run-tests`, `/status`, `/explore-network` |
| `.gitignore` / `.gitattributes` | Git entries for `.toe`/`.tox` binary handling and externalized file tracking |

Regenerate at any time:

```python
op.Embody.InitEnvoy()   # Regenerate MCP + AI client config
op.Embody.InitGit()     # Regenerate git config, then re-run InitEnvoy
```

## Compatible Clients

Envoy works with any MCP client:

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI and VS Code extension)
- [Cursor](https://www.cursor.com/)
- [Windsurf](https://windsurf.com/)
- Any other client that supports the MCP protocol
