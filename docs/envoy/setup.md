# Envoy Setup

## Prerequisites

You'll need:

- **TouchDesigner 2025.32280** or later
- An MCP-compatible client such as [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Cursor](https://www.cursor.com/), or [Windsurf](https://windsurf.com/)

Embody automatically installs all server-side dependencies (`mcp`, `uvicorn`, etc.) when Envoy is first enabled — no manual Python setup required. This first install (and any later dependency upgrade) runs **in a background thread** so TouchDesigner stays responsive; the Embody COMP shows `Installing deps... (one-time)` while it works and switches to `Running on port …` once MCP is ready. After that, every startup takes the fast path and skips the install entirely. Envoy validates the virtual environment on each startup and falls back to the system Python if the venv is broken (see [Broken Virtual Environment](troubleshooting.md#broken-virtual-environment)).

## Enabling Envoy

1. **Enable Envoy**: Toggle the **Envoy Enable** parameter on the Embody COMP
2. **Server starts**: Envoy runs on `localhost:9870` (configurable via **Envoy Port**)
3. **Auto-configuration**: Envoy creates `.mcp.json` and AI client config files in your git repo root (if available) or project folder. If your project is in a git repo, Envoy also generates `.gitignore` and `.gitattributes` entries.
4. **Connect your MCP client**: Start a new Claude Code session (or restart your IDE) — it picks up the `.mcp.json` automatically

## Regenerating Config Files

You can regenerate Envoy's config files at any time from the TD textport or a script:

```python
op.Embody.InitEnvoy()   # Regenerate MCP + AI client config
op.Embody.InitGit()     # Init/reconnect git repo + .gitignore/.gitattributes
```

Use `InitEnvoy()` after updating Embody, changing the AI Client parameter, or if config files were accidentally deleted. Use `InitGit()` after creating a git repo manually, or to refresh `.gitignore`/`.gitattributes` entries. `InitGit()` also calls `InitEnvoy()` to update paths.

## Manual Configuration

If you prefer manual control, create `.mcp.json` in your project directory. You can use either the direct HTTP transport or the STDIO bridge:

**HTTP transport** (simpler, requires TD to be running):

```json
{
  "mcpServers": {
    "envoy": {
      "type": "http",
      "url": "http://localhost:9870/mcp"
    }
  }
}
```

**STDIO bridge** (recommended — supports launching TD from Claude Code):

```json
{
  "mcpServers": {
    "envoy": {
      "type": "stdio",
      "command": "python3",
      "args": ["-u", ".embody/envoy-bridge.py", "--port", "9870",
               "--config", ".embody/envoy.json"]
    }
  }
}
```

The STDIO bridge provides meta-tools (`get_td_status`, `launch_td`, `restart_td`) that work even when TouchDesigner is not running. See [Claude Code Integration](claude-code.md#stdio-bridge) for details.

## Changing the Port

Change the **Envoy Port** parameter on the Embody COMP. If the server is running, it automatically:

1. Stops the server on the old port
2. Restarts on the new port (after a 2-frame delay for clean shutdown)
3. Updates `.mcp.json` with the new port

If the server is not running, changing the port simply updates the parameter value.

## Running Multiple Instances

You can run multiple TouchDesigner instances with Envoy enabled in the same git repo. Each instance automatically claims a unique port from the range `[base_port, base_port + 9]` (default: 9870–9879).

To switch between instances from Claude Code, use the `switch_instance` bridge meta-tool. See [Claude Code Integration](claude-code.md#working-with-multiple-instances) for usage details and [Architecture](architecture.md#multiple-instances) for how it works.

!!! tip
    Running two instances of the **same `.toe` file** works out of the box — Envoy auto-suffixes the registry key (`MyProject`, `MyProject-2`, etc.).

## Claude Code Integration

When Envoy starts, it generates a full Claude Code configuration in your project root:

- **`CLAUDE.md`** — project context and critical rules
- **`.claude/rules/`** — always-loaded conventions (TD Python, network layout, MCP safety)
- **`.claude/skills/`** — on-demand workflow guides (operator creation, debugging, externalization)

Pristine generated files are refreshed each time Envoy starts to stay up to date. If you edit a generated rule or skill, Embody detects the change (via a content hash in `.embody/generated-hashes.json`) and keeps your version instead of overwriting it — delete the file to opt back into regeneration. See [Claude Code Integration](claude-code.md) for the full reference.

## MCP Tool Permissions

When Envoy is first enabled, it deploys a `.claude/settings.local.json` file that **auto-authorizes all Envoy MCP tools** — including write operations like `create_op`, `delete_op`, `execute_python`, and `set_dat_content`. This means your AI assistant can act without per-tool confirmation prompts.

If you prefer finer control, edit `.claude/settings.local.json` in your project root after setup. The `allow` array lists tool permission patterns — remove any tools you want Claude Code to prompt you for before executing.

For example, to allow only read-only tools and require confirmation for write operations, keep only the `query_*` and `get_*` entries in the allow list.

## Fresh Clones and TD Version Matching

When you clone a repo someone else built with Embody, the `.embody/envoy.json` file (which records the local TD install path) is gitignored — the path it references won't exist on your machine. Embody handles this by also committing `.embody/project.json`, which records the **TouchDesigner build the project was last saved with** (e.g., `{"td_build": "2025.32660"}`).

The first time the bridge needs to launch TD on a fresh clone, it reads `td_build` from `project.json`, scans your standard TouchDesigner install locations (`/Applications/TouchDesigner*.app` on macOS, `C:\Program Files\Derivative\TouchDesigner.*` on Windows, `/opt/derivative/touchdesigner-*` on Linux), and picks the matching install — exact-build match if you have it, otherwise the closest same-year build (with a warning). If nothing matches, the error response includes the Derivative download link and the exact build number you need.

Backward compatible — projects without `project.json` use `envoy.json`'s `td_executable` exactly as before. See [Architecture](architecture.md#embodyprojectjson-build-pin-committed) for the full match policy.

## Verifying the Connection

After starting Envoy and your MCP client:

1. The Embody COMP should show **Envoy Enable** toggled on and a status indicator
2. Your MCP client should list the Envoy tools (e.g., `create_op`, `get_op`, `set_parameter`)
3. Try a simple command like "list all operators in the project" to verify the connection
