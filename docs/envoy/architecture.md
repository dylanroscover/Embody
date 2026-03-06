# Architecture

## Dual-Thread Design

Envoy uses a two-thread architecture to bridge the MCP protocol with TouchDesigner's single-threaded main loop:

- **Worker thread**: Runs the MCP server (FastMCP with Streamable HTTP transport via uvicorn) — no TouchDesigner imports allowed
- **Main thread**: Executes all TD operations via `EnvoyExt._onRefresh()` callback
- **Communication**: `threading.Event` + `Queue` for request/response between threads
- **Thread management**: Uses `op.TDResources.ThreadManager` (TDTask pattern)

```
┌──────────────────┐     Event + Queue     ┌──────────────────┐
│   Worker Thread   │ ◄──────────────────► │   Main Thread     │
│                   │                       │                   │
│  FastMCP Server   │    MCP Request ──►   │  _onRefresh()     │
│  (uvicorn)        │                       │  Execute TD ops   │
│                   │   ◄── TD Result       │                   │
│  127.0.0.1:9870   │                       │  Cook cycle       │
└──────────────────┘                       └──────────────────┘
```

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

## Thread Safety Rules

!!! danger "Critical"
    The worker thread (`EnvoyMCPServer`) must **never** import or call TouchDesigner modules. All TD access goes through `_execute_in_td()` which routes operations to the main thread.

- All TD operations execute on the main thread via the `_onRefresh()` callback
- The worker thread only handles HTTP/MCP protocol logic
- Data crosses the thread boundary only through the `Queue` objects
- `op.TDResources.ThreadManager` must not be accessed from within a worker thread (triggers THREAD CONFLICT)

## Error Handling

Envoy handles two error categories:

1. **Protocol errors** (JSON-RPC level) — unknown tools, invalid arguments, or server errors. FastMCP handles these automatically.
2. **Tool execution errors** — returned in tool results via `{'error': str(e)}` dicts. These indicate the tool ran but encountered a problem (missing operator, invalid path, etc.).

All tool handlers validate inputs before passing to TD operations and return structured error information rather than raising exceptions.
