---
name: multi-instance
description: "Workflow for running multiple TD instances with Envoy, switching between them, and understanding the instance registry."
---

# Multi-Instance Workflow

Envoy supports multiple TouchDesigner instances running simultaneously in the same git repo. Each instance gets its own port, and the bridge can switch between them on demand.

## Architecture

```
Claude Code  <-->  STDIO Bridge  <-->  Envoy (TD instance A, port 9870)
                        |
                        +---switch--->  Envoy (TD instance B, port 9871)
```

- **One bridge process per SESSION** (spawned by each AI client's MCP client)
- **Per-session pinning** — each bridge pins to an instance NAME and re-resolves its port from the registry every tick, so a pinned instance restarting on a new port still self-heals. The registry `active` field only seeds NEW bridges without a pin.
- **Switching is instant and session-local** — `switch_instance` re-pins THIS session's bridge in-memory; peers are untouched unless you pass `all_sessions=True` (writes the registry default and bumps `active_epoch`, which moves every session)
- **Registration never re-routes running sessions** — a new instance takes the `active` default slot only when it is vacant or names a dead instance

## Instance Registry (`.embody/envoy.json`)

Each Envoy instance registers itself in `.embody/envoy.json` at the git root on startup:

```json
{
  "active": "Embody-5.257",
  "td_executable": "/Applications/TouchDesigner.app",
  "instances": {
    "Embody-5.257": {
      "toe_path": "dev/Embody-5.257.toe",
      "port": 9870,
      "td_pid": 12345
    },
    "MySecondProject": {
      "toe_path": "MySecondProject.toe",
      "port": 9871,
      "td_pid": 67890
    }
  }
}
```

| Field | Purpose |
|-------|---------|
| `active` | Default instance for NEWLY-spawned bridges (each bridge then pins per-session; only an `active_epoch` bump re-targets pinned bridges) |
| `td_executable` | Path to TD app (used by `launch_td`) |
| `instances.<name>.toe_path` | Relative path to the `.toe` file |
| `instances.<name>.port` | Envoy HTTP port for this instance |
| `instances.<name>.td_pid` | OS process ID of the TD process |

Instances are added on Envoy startup and removed on graceful shutdown (`onDestroyTD`).

## Port Allocation

Each instance picks a port from a 10-port range starting at the configured `Envoyport` parameter (default: `9870`):

1. Try the base port first
2. If occupied by **this** TD process's old server, force-close it and reclaim
3. If occupied by **another** process, scan `base+1` through `base+9`
4. First available port wins; if all 10 are taken, startup fails

This means up to 10 simultaneous instances per base port.

## Workflow: Running Multiple Instances

### 1. Open the first `.toe` file

Launch TD normally or via `launch_td`. Envoy starts on its configured port and registers in `.embody/envoy.json`.

### 2. Open additional `.toe` files

Open them in separate TD instances (File > Open or double-click). Each Embody/Envoy auto-starts and claims the next available port.

### 3. List instances

Call `switch_instance` with no parameters to see all registered instances and their status:

```
switch_instance()
```

Returns each instance's name, port, PID, reachability (PID alive + port responding), and whether it's the active target.

### 4. Switch to a different instance

```
switch_instance(instance="MySecondProject")
```

The bridge immediately redirects to the target instance's port. All subsequent MCP calls go to that TD process.

### 5. Switch back

```
switch_instance(instance="Embody-5.257")
```

## Reachability Checks

An instance is **reachable** only when:
- Its registered PID is alive (`kill -0` / process table check)
- Its registered port responds to a TCP connect

Both conditions must be true. A dead PID with an open port means another instance reused that port — the entry is stale.

`get_td_status` includes the full instance registry with reachability in its response.

## Stale Instance Cleanup

Instances are deregistered on graceful TD shutdown. If TD crashes:
- The PID becomes dead, so the instance shows as unreachable
- The port may be freed, allowing a new instance to claim it
- Stale entries remain in `.embody/envoy.json` but are filtered by reachability checks
- Re-launching TD with the same `.toe` overwrites the stale entry

## Closing Instances

**Preferred**: Close TD instances via Envoy by calling `execute_python` with `project.quit()`. This prompts the user to save unsaved changes, then triggers `onDestroyTD` for clean deregistration and port release. Works reliably across platforms.

To close a specific instance, `switch_instance` to it first, then send the quit command:

```python
switch_instance(instance="MySecondProject")   # target the instance
execute_python(code="project.quit()")         # user gets save prompt in TD
```

**Never use `project.quit(force=True)`** unless the user has explicitly asked — it skips the save dialog and risks losing unsaved work.

**Avoid**: `osascript -e 'quit app "TouchDesigner"'` and similar OS-level approaches are unreliable — they may not target the correct instance and don't guarantee clean Envoy shutdown.

## Common Scenarios

| Scenario | Action |
|----------|--------|
| Test code in two `.toe` files side by side | Open both, `switch_instance` between them |
| Test code in a secondary `.toe` | Open it, switch to it, perform work, switch back |
| Check if a second TD is still running | `switch_instance()` (list mode) or `get_td_status` |
| One instance crashed, want the other | `switch_instance` to the surviving instance |
| Close a specific instance | `switch_instance` to it, then `execute_python` with `project.quit()` |

## Same-Project Instances

When you open the same `.toe` file in multiple TD instances, Envoy auto-suffixes the registry key to avoid collisions. The first instance registers as `MyProject`, the second as `MyProject-2`, etc. Stale entries (dead PIDs) are automatically reclaimed.

## Limitations

- The bridge connects to **one instance at a time** — no parallel MCP calls to multiple instances
- Maximum **10 instances** per base port range
- `.embody/envoy.json` is per git root — instances in different repos have separate registries
- `launch_td` always launches the `.toe` configured in `.embody/envoy.json` top-level `toe_path` — use TD directly to open additional files
- Opening the same `.toe` file in multiple instances auto-suffixes keys (`MyProject-2`, `-3`, etc.)
