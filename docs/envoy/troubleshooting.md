# Envoy Troubleshooting

## Server Won't Start

**Symptoms:** Toggling Envoy Enable does nothing, no port number appears in the toolbar, or errors in the Textport.

1. **Check the Textport** (Alt+T) — Embody logs all startup messages there. Look for lines starting with `[Envoy]`.
2. **Dependency install failed:** Envoy installs `mcp`, `uvicorn`, and other packages on first enable. If this fails (e.g., no internet, Python version mismatch), the server can't start. Check for pip errors in the Textport and try installing manually:
   ```
   pip install mcp uvicorn httpx pydantic
   ```
3. **Port already in use:** If another process is using port 9870 (the default), the server will fail to bind. Change the **Envoy Port** parameter on the Embody COMP to a different port (e.g., 9871).
4. **TD version too old:** Envoy requires TouchDesigner **2025.32280** or later.

## Claude Code Can't Connect

**Symptoms:** Claude Code says "MCP server not found" or tool calls time out.

1. **Verify Envoy is running:** Check that the Embody toolbar shows a port number next to the Envoy toggle. If not, see "Server Won't Start" above.
2. **Check `.mcp.json`:** Look for `.mcp.json` in your git repo root. It should contain a server entry for `envoy` with the correct port. If it's missing:
    - Make sure your `.toe` project is inside a git repository
    - Re-enable Envoy (toggle off, then on) to regenerate it
    - Or create it manually — see [Manual Configuration](setup.md#manual-configuration)
3. **Restart Claude Code:** After Envoy generates `.mcp.json`, you need to start a **new** Claude Code session for it to pick up the config. Run `claude` again in your project directory.
4. **Port mismatch:** Ensure the port in `.mcp.json` matches the Envoy Port parameter in TD. If you changed the port, `.mcp.json` should update automatically — but check it.
5. **Firewall or proxy:** Envoy binds to `127.0.0.1` (localhost only). If you're running Claude Code on the same machine, firewalls shouldn't be an issue. If using a remote setup, Envoy does not support remote connections.

## Git Initialization Failed

**Symptoms:** Envoy starts but `.mcp.json`, `CLAUDE.md`, and `.claude/` files are not generated.

1. **No git repo:** Envoy needs a git repository to know where to place config files. If your project isn't in a repo, either:
    - Run `git init` in your project directory
    - Or select "Start Without Git" and [configure manually](setup.md#manual-configuration)
2. **Git init error:** If Envoy attempted to initialize git and failed, a dialog will explain the error. Common causes:
    - `git` not on your system PATH
    - Permissions issue in the project directory
    - TouchDesigner's embedded Python environment conflicting with git (Envoy strips known problematic env vars, but edge cases exist)
3. **Verify manually:** Open a terminal in your project directory and run `git rev-parse --is-inside-work-tree`. If this returns `true`, git is working and you can re-enable Envoy.

## Curl Fallback Not Working

If Claude Code can't use MCP transport, it may try to reach Envoy via curl. Verify the server is reachable:

```bash
curl http://localhost:9870/mcp
```

You should get a response (even if it's an error about missing JSON body). If you get "connection refused," the server isn't running or is on a different port.

## MCP Disconnects Mid-Session

**Symptoms:** Claude Code stops being able to call Envoy tools mid-conversation — tool calls fail with connection errors or timeouts, even though TD is still running and Envoy shows as enabled.

This can happen if TouchDesigner restarts, the Envoy server cycles, or the bridge process exits unexpectedly.

### Claude Code CLI

Restart the session by exiting (`Ctrl+C` or `/exit`) and running `claude` again in your project directory. The bridge will reconnect to Envoy automatically.

### Claude Code VS Code Extension

1. **Close the conversation tab** in the editor.
2. Open the **Claude Code sidebar** (click the Claude icon in the Activity Bar on the left).
3. Click the conversation to reopen it — your full message history is restored.

Reopening the conversation re-initializes the MCP connection. The message history is preserved, so you can continue where you left off.

!!! tip
    If you want a completely fresh start instead, type `/clear` in the conversation. This wipes the message history but keeps the tab open with a new MCP connection.

## Multiple Instances

### Wrong instance responding

**Symptoms:** MCP tool calls affect a different TD project than expected, or `execute_python` returns unexpected `project.name`.

1. **Check which instance is active**: Call `switch_instance` with no arguments — it lists all registered instances and marks which one the bridge is targeting.
2. **Switch to the correct one**: Call `switch_instance` with the instance name (`.toe` filename without the extension).
3. **Stale entries**: If an instance shows as "reachable" but you've already closed it, the registry entry is stale. Restarting Envoy in the running instance will clean it up.

### Same-file instance naming

**Symptoms:** You opened the same `.toe` file in two TD instances and want predictable names for `switch_instance`.

Envoy auto-suffixes duplicate keys (`MyProject`, `MyProject-2`, etc.), so both instances are addressable. If you want predictable names instead of auto-suffixed ones, set the **Instance Name** parameter (`Envoyinstancename`) on each Embody COMP before starting Envoy.

### Port exhaustion

**Symptoms:** Envoy fails to start with a message about no available ports.

Envoy scans 10 ports (default: 9870–9879). If all are occupied, it can't start. Close unused TD instances or change the **Envoy Port** parameter to a different base (e.g., 9880).

## Log Files

Embody writes detailed logs to `dev/logs/` in your project directory. Check the most recent `Embody-*.log` file for the full picture — the Textport ring buffer only holds 200 entries.
