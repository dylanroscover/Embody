# Envoy Setup

## Prerequisites

You'll need:

- **TouchDesigner 2025.32280** or later
- An MCP-compatible client such as [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Cursor](https://www.cursor.com/), or [Windsurf](https://windsurf.com/)

Embody automatically installs all server-side dependencies (`mcp`, `uvicorn`, etc.) when Envoy is first enabled — no manual Python setup required.

## Enabling Envoy

1. **Enable Envoy**: Toggle the **Envoy Enable** parameter on the Embody COMP
2. **Server starts**: Envoy runs on `localhost:9870` (configurable via **Envoy Port**)
3. **Auto-configuration**: Envoy creates a `.mcp.json` file in your git repo root automatically
4. **Connect your MCP client**: Start a new Claude Code session (or restart your IDE) — it picks up the `.mcp.json` automatically

!!! tip
    Auto-configuration requires your `.toe` project to be inside a **git repository** (see [Project Setup](../embody/getting-started.md#project-setup)). If your project isn't in a git repo, you'll need to create the config file manually.

## Manual Configuration

If your project isn't in a git repo, create `.mcp.json` manually in your project directory:

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

## Changing the Port

Change the **Envoy Port** parameter on the Embody COMP. If the server is running, it automatically:

1. Stops the server on the old port
2. Restarts on the new port (after a 2-frame delay for clean shutdown)
3. Updates `.mcp.json` with the new port

If the server is not running, changing the port simply updates the parameter value.

## CLAUDE.md Auto-Generation

When Envoy starts, it generates a `CLAUDE.md` file in your project root. This file provides Claude Code with context about:

- TouchDesigner development patterns and best practices
- The MCP tool reference
- Testing conventions
- Project-specific guidance

This file is regenerated each time Envoy starts to stay up to date.

## Verifying the Connection

After starting Envoy and your MCP client:

1. The Embody COMP should show **Envoy Enable** toggled on and a status indicator
2. Your MCP client should list the Envoy tools (e.g., `create_op`, `get_op`, `set_parameter`)
3. Try a simple command like "list all operators in the project" to verify the connection
