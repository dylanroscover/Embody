# TouchDesigner Connectivity

## Session Start -- ALWAYS do this first

Before any MCP tool call, verify TD is running and reachable:

1. **Check for MCP tools**: Search for `get_td_status` or any Envoy tool. The bridge v2 disk cache means tools are always available at session start -- even if TD is down, bridge meta-tools (`get_td_status`, `launch_td`, `restart_td`, `switch_instance`) are served from cache.
2. **If tools exist**: Call `get_td_status`. If TD is not running, call `launch_td`. The bridge's reconciler auto-detects TD state changes every 1-5 seconds and handles reconnection automatically.
3. **If NO Envoy tools exist at all**: this is a fresh clone or a handed-over project, NOT a dead end. `.mcp.json` is gitignored -- it holds absolute paths to one machine's venv and bridge -- so it never travels with the repo, while `CLAUDE.md` / `AGENTS.md` / the rules DO. Never conclude "this project has no path into TouchDesigner" from a missing `.mcp.json`. Tell the user: open the project's `.toe` in TouchDesigner and restart this session once the Embody toolbar shows a port. Envoy starts itself when the committed `.embody/project.json` declares it; if no port appears, they turn on **Enable Envoy** on the Embody COMP.

## Self-Heal First

Connectivity self-heals at two independent layers: the bridge reconciler reconnects the client-side STDIO bridge to a live Envoy, and the TD-side Envoy liveness watchdog revives an enabled-but-down server socket without restarting TD.

- If `connected:false` while `td_process_alive:true`, WAIT ~10s and re-check `get_td_status`; do NOT `restart_td`, relaunch TD, or toggle Envoy unless it genuinely has not recovered after ~15s.
- Editing an extension `.py` (EnvoyExt / EmbodyExt / TDXNExt) does NOT need a restart; source DATs have `syncfile=True` and reinit on change.
- Connectivity broken beyond ~15s of self-heal waiting -> MUST load /td-recovery before manual intervention.
