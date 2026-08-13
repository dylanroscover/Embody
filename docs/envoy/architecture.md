# Architecture

## Dual-Thread Design

Envoy uses a two-thread architecture to bridge the MCP protocol with TouchDesigner's single-threaded main loop:

- **Worker thread**: Runs the MCP server (MCP SDK 2.x `MCPServer` with Streamable HTTP transport via uvicorn) — no TouchDesigner imports allowed
- **Main thread**: Executes all TD operations via `EnvoyExt._onRefresh()` callback
- **Communication**: `threading.Event` + `Queue` for request/response between threads
- **Thread management**: Uses `op.TDResources.ThreadManager` (TDTask pattern)

```
┌──────────────┐  STDIO   ┌──────────────┐  HTTP   ┌──────────────┐  Event+Queue  ┌──────────────┐
│  Claude Code  │ ◄──────► │ STDIO Bridge  │ ──────► │ Worker Thread │ ◄───────────► │  Main Thread  │
│  (MCP client) │  JSON-RPC │              │  POST   │              │               │              │
│              │          │ envoy-bridge  │         │ MCPServer    │  MCP Request  │ _onRefresh() │
│              │          │ .py           │         │ (uvicorn)    │  ──────────►  │ Execute TD   │
│              │          │              │         │              │               │ operations   │
│              │          │ Meta-tools:  │         │ 127.0.0.1    │  ◄── Result   │              │
│              │          │ get_td_status │         │ :9870        │               │ Cook cycle   │
│              │          │ launch_td    │         │              │               │              │
│              │          │ restart_td   │         │              │               │              │
└──────────────┘          └──────────────┘         └──────────────┘               └──────────────┘
```

## Liveness Watchdog

A self-healing watchdog keeps Envoy reachable across the events that would otherwise drop the connection — a `project.save()` (which reinitializes the extension) or an extension reload. It is a pure `run()`-loop, armed once per `EnvoyExt` instance (in `__init__`, not `Start()`), so it survives those reinits.

- **Probes the socket** every ~4 seconds.
- **Revives when enabled-but-down** — a dead listener while TD keeps running, or a save/reinit that took the server down. It force-frees port 9870 if it is still held and rebinds within ~1 second, with no operator, no timer, and no manual toggle.
- **Self-limited** — a generation token collapses leftover ticks on the next reinit, and a short cooldown prevents revive storms during the save strip/restore burst.

This is independent of the bridge's own reconciler: the watchdog revives the TD-side server, the bridge reconnects the STDIO bridge to it. Between the two layers, a dropped connection almost never needs manual recovery.

## STDIO Bridge

Claude Code communicates with MCP servers via STDIO (stdin/stdout JSON-RPC). Since Envoy runs as an HTTP server inside TouchDesigner, the bridge script (`.embody/envoy-bridge.py`) translates between these transports.

The bridge provides several features beyond simple proxying:

- **Local MCP handshake**: Handles `initialize`, `notifications/initialized`, and `tools/list` locally when TD is not running, so Claude Code can always complete the MCP setup and discover bridge meta-tools
- **Meta-tools**: `get_td_status`, `launch_td`, `restart_td`, `switch_instance`, and the 16 `convoy_*` tools run entirely on the bridge — no Envoy connection required (Convoy tools talk to the local host app instead)
- **Automatic retry**: Transient connection failures are retried with exponential backoff before reporting an error
- **Crash detection**: Tracks the TD process PID and detects when it exits unexpectedly
- **Crash-loop protection**: Limits launches to 3 within a 5-minute window to prevent infinite restart loops
- **Orphan cleanup**: A watchdog thread terminates the bridge if its parent process (Claude Code) exits, preventing zombie processes
- **Stale bridge cleanup**: On startup, kills other bridge processes targeting the same port from previous sessions

The bridge is regenerated from Embody's templates on each Envoy start. It uses only the Python standard library (no third-party dependencies).

### `.embody/envoy.json` Configuration

The bridge reads project configuration from `.embody/envoy.json`, co-located with `.mcp.json` at the AI Project Root (the git root by default, or the project folder / a custom path per Embody's **AI Project Root** parameter). This file also serves as the **instance registry** when running multiple TouchDesigner instances.

```json
{
  "active": "MyProject",
  "td_executable": "/Applications/TouchDesigner.app",
  "instances": {
    "MyProject": {
      "toe_path": "dev/MyProject.toe",
      "port": 9870,
      "td_pid": 12345
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `active` | Name of the instance the bridge currently targets |
| `td_executable` | Path to the TouchDesigner executable or `.app` bundle |
| `instances.<name>.toe_path` | Relative path to the `.toe` project file |
| `instances.<name>.port` | Envoy's HTTP port for this instance |
| `instances.<name>.td_pid` | OS process ID of the TouchDesigner process |

Each Envoy instance registers itself on startup and deregisters on graceful shutdown. The instance key is derived from `project.name` (the `.toe` filename). This file is auto-generated by Envoy and added to `.gitignore`.

### `.embody/local.json` Build Pin (machine-local) and `.embody/project.json` (committed)

`.embody/envoy.json` is gitignored — its `td_executable` path is local to whoever last ran the project. The bridge's preferred signal for picking a TouchDesigner install is the **machine-local build pin** in `.embody/local.json`:

```json
{
  "td_build": "2025.32660"
}
```

Embody writes this file on `onProjectPostSave` and once at startup (frame 80), idempotent so unchanged builds skip the write. It is **gitignored** like the rest of `.embody/` — the pin used to live in the committed `project.json`, but a committed pin churns whenever collaborators run different TD builds, so it was retired (Embody removes a legacy `td_build` key from `project.json` automatically; a still-present legacy key is tolerated as a fallback by the bridge).

`.embody/project.json` remains **committed** (`.embody/*` + `!.embody/project.json` in `.gitignore`) as the home for project-level metadata that genuinely travels with the repo. Embody stewards it with key-level ownership: it only ever touches keys it owns, preserves everything else, and never overwrites a file it cannot parse.

When `launch_td` runs, the bridge reads the pin (local first, legacy committed key second), scans the platform's standard install locations, and picks a TouchDesigner:

- **macOS**: `/Applications/TouchDesigner*.app` (parses `CFBundleShortVersionString` from each `Info.plist`)
- **Windows**: `C:\Program Files\Derivative\TouchDesigner.*`
- **Linux**: `/opt/derivative/touchdesigner-*`

**Match policy**, in order:

1. **Exact build match** — silent
2. **Same-year closest build** — warns (parameter defaults can drift between builds within a year)
3. **`envoy.json`'s `td_executable` if it still exists locally** — warns
4. **Newest installed TD** — warns
5. **Nothing found** — error response with the Derivative download link and the pinned build number

Backward compatible. With no pin anywhere (a fresh clone where TD has never run), the policy still resolves: `envoy.json`'s `td_executable` if it exists locally, otherwise the newest installed TouchDesigner.

### Multiple Instances

Envoy supports running multiple TouchDesigner instances simultaneously in the same git repo. Each instance gets its own port via automatic port allocation.

**Port allocation**: Each instance picks a port from a 10-port range starting at the configured Envoy Port (default: 9870). If the base port is occupied by another instance, Envoy scans ports `base+1` through `base+9` and claims the first available one. Up to 10 simultaneous instances are supported per base port.

**Bridge routing**: The STDIO bridge connects to **one active instance** at a time. The `switch_instance` meta-tool is **session-local by default**: it re-pins only this session's bridge to the new instance's port in memory, leaving `.embody/envoy.json` and every peer session untouched. Pass `all_sessions=true` to also write the new `active` field (and bump `active_epoch`, which overrides peers' own pins) — that is how you move the whole-user default. Switching is instant — no reconnection delay.

**Instance reachability**: The bridge verifies instances by checking both PID liveness and port responsiveness. An instance is only considered reachable when both checks pass. This filters out stale registry entries from crashed or closed instances.

## Design Decisions

### Stateless HTTP Transport

Envoy uses `stateless_http=True` because TouchDesigner's single-threaded model means concurrent sessions would queue on the same main-thread execution path anyway. Stateless mode simplifies the implementation and avoids session management overhead.

### 30-Second Operation Timeout

`_execute_in_td()` times out at 30 seconds. This prevents indefinite hangs if the main thread is blocked (e.g., modal dialog), while allowing enough time for heavy operations like `.tox` saves. If a TD operation takes longer, the MCP tool returns a timeout error — the operation may need to be broken into smaller steps.

### Localhost-Only Binding

Envoy binds exclusively to `127.0.0.1` as a security requirement. Binding to `0.0.0.0` would expose the MCP server to the local network and enable DNS rebinding attacks from malicious websites.

### Standalone Thread

The MCP server runs as a `standalone=True` TDTask because it is long-lived (runs for the entire session), unlike pool tasks which are meant for short-lived work units.

### Queue-Based Communication

Uses `threading.Event` + `Queue` rather than locks because TD's cook cycle is frame-based — the main thread can only process requests once per frame via the RefreshHook.

## Graceful Shutdown

The shutdown sequence ensures clean port release:

1. `EnvoyExt.Stop()` is called (from UI toggle, `onExit`, or project close)
2. `shutdown_event.set()` signals the worker thread's uvicorn server to stop
3. Uvicorn completes its shutdown (stops accepting connections, drains existing)
4. Worker thread's target function returns
5. `SuccessHook` or `ExceptHook` fires on the main thread for cleanup
6. Port is released and available for rebinding

### Closing Instances via MCP

The preferred way to close a TD instance programmatically is via Envoy itself:

```python
execute_python(code="project.quit()")
```

This triggers TD's normal quit flow — the user is prompted to save unsaved changes, then `onDestroyTD` fires, the instance deregisters from `.embody/envoy.json`, and the port is released cleanly. This is more reliable than OS-level approaches (`osascript`, `taskkill`) which may not trigger TD's shutdown callbacks.

!!! warning
    Never use `project.quit(force=True)` unless the user has explicitly asked for it — `force=True` skips the save dialog and risks losing unsaved work.

To close a specific instance in a multi-instance setup, first `switch_instance` to target it, then send the quit command.

## Thread Safety Rules

!!! danger "Critical"
    The worker thread (`EnvoyMCPServer`) must **never** import or call TouchDesigner modules. All TD access goes through `_execute_in_td()` which routes operations to the main thread.

- All TD operations execute on the main thread via the `_onRefresh()` callback
- The worker thread only handles HTTP/MCP protocol logic
- Data crosses the thread boundary only through the `Queue` objects
- `op.TDResources.ThreadManager` must not be accessed from within a worker thread (triggers THREAD CONFLICT)

## Error Handling

Envoy handles two error categories:

1. **Protocol errors** (JSON-RPC level) — unknown tools, invalid arguments, or server errors. The MCP SDK's `MCPServer` handles these automatically.
2. **Tool execution errors** — returned in tool results via `{'error': str(e)}` dicts. These indicate the tool ran but encountered a problem (missing operator, invalid path, etc.).
3. **Recovery hints** — when a tool returns an `{'error': ...}` result, Envoy auto-attaches a `recovery_hints` list (each entry `{cause, action, next_tools}`), matched against the real error string by a small curated table (path-not-found -> `query_network`/`find_children`; parameter-not-found -> `get_op`; wrong family; empty capture -> `get_op_performance`; thread conflict; timeout -> `get_project_performance`). Attached centrally in `_send_response` — additive, never clobbers an existing block, never raises — it steers the agent's next call instead of a blind retry of the same failing tool.

All tool handlers validate inputs before passing to TD operations and return structured error information rather than raising exceptions.
