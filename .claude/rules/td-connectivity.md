# TouchDesigner Connectivity

## Session Start — ALWAYS do this first

Before any MCP tool call, verify TD is running and reachable:

1. **Check for MCP tools**: Search for `get_td_status` or any Envoy tool. The bridge v2 disk cache means tools are always available at session start — even if TD is down, bridge meta-tools (`get_td_status`, `launch_td`, `restart_td`, `switch_instance`) are served from cache.
2. **If tools exist**: Call `get_td_status`. If TD is not running, call `launch_td`. The bridge's reconciler auto-detects TD state changes every 1-5 seconds and handles reconnection automatically.

## How the Bridge Works (v2)

The bridge runs a background reconciler thread that continuously manages connectivity:

- **Config polling** (every 1s): Watches `.embody/envoy.json` for mtime changes. Automatically switches to the new active instance when the config is updated.
- **Heartbeat** (every 3-30s, dynamic): Pings the backend to detect connect/disconnect transitions. Fast cadence (3s) while disconnected, slow cadence (30s) once stable.
- **Process discovery**: Detects new and exited TD processes via `find_all_td_pids()`. Forces a config re-read when new TDs appear.
- **Tool cache**: Persists the tool list to disk so new sessions start with full tools immediately, without waiting for a backend round-trip.
- **Single-attempt forwarding**: Failed requests return an error immediately — no per-request retry loop. The reconciler handles recovery in the background.

## Recovery — Manual Intervention

Most connectivity issues are now handled automatically by the bridge. Manual recovery is only needed when the bridge process itself is broken.

1. **Call `get_td_status`**: This is always available (even when TD is down). It shows connection state, process liveness, instance registry, and any unregistered TD processes.
2. **If TD is not running**: Call `launch_td`. The bridge will launch TD with the configured `.toe` file and wait for Envoy to become reachable.
3. **If the wrong instance is active**: Call `switch_instance` to list or switch instances. The reconciler also auto-switches when `.embody/envoy.json` is edited.
4. **If the bridge process is stuck**: Tell the user to **reopen this session/conversation** — this is always the first recovery step. Only if that fails, suggest restarting the MCP server as a fallback.

### Common failure: stale active instance

The most frequent cause of connectivity issues is `.embody/envoy.json` having `active` set to an instance whose TD process is no longer running. The bridge's reconciler detects this automatically via heartbeat failures and reports it in `get_td_status`. Use `launch_td` or `switch_instance` to recover.

### Common failure: broken venv

**Symptoms**: Bridge process doesn't start at all, or starts and immediately exits. `dev/logs/envoy-bridge.log` is empty or shows a Python traceback about a missing interpreter. `.mcp.json` command points to a `.venv/` Python that doesn't work.

**Cause**: The venv was created from a TD Python installation that has since been upgraded or removed. The `home` key in `.venv/pyvenv.cfg` points to a dead path (common on Windows with versioned TD directories like `TouchDesigner.2025.32460/`).

**Diagnosis**:
1. Read `.mcp.json` — find the `command` path for the envoy server.
2. Test it: run `<command> -c "print(1)"` via Bash. If it fails with "No Python at ..." or similar, the venv is broken.
3. Confirm by reading `.venv/pyvenv.cfg` — check if the `home` path points to an existing directory.

**Fix**:
1. Delete the broken venv: `rm -rf <project_dir>/.venv`
2. Envoy will recreate it on next startup. Tell the user to toggle Envoy off and on in TD, or restart TD.
3. After recreation, reopen the Claude Code session so the bridge reconnects with the new venv Python.

**Prevention**: Envoy validates the venv Python on startup and logs a warning if broken. Check TD textport for "failed to execute" warnings after TD upgrades.
