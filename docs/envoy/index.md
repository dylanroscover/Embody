# Envoy MCP Server

**Envoy** is an embedded [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) server that lets AI coding assistants interact with TouchDesigner programmatically. With Envoy running, an MCP-compatible client can create operators, set parameters, wire connections, export networks, manage externalizations, and more — all through natural language conversation.

## Compatible Clients

Envoy works with any MCP client, including:

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI and VS Code extension)
- [Cursor](https://www.cursor.com/)
- [Windsurf](https://windsurf.com/)
- Any other client that supports the MCP protocol

## Capabilities

Envoy exposes **40+ MCP tools** organized into categories:

| Category | Examples |
|----------|---------|
| **Operator Management** | Create, delete, copy, rename, query operators |
| **Parameters** | Get/set values, expressions, bind expressions |
| **Connections** | Wire operators together, disconnect inputs |
| **DAT Content** | Read/write text and table data |
| **Extensions** | Create TD extensions with proper boilerplate |
| **Annotations** | Create network boxes, comments, annotate groups |
| **Diagnostics** | Check errors, get performance data, introspect API |
| **Embody Integration** | Tag, save, query externalizations |
| **TDN Export/Import** | Export/import network snapshots as JSON |
| **Code Execution** | Run arbitrary Python in TouchDesigner |

For the complete tool reference, see [Tools Reference](tools-reference.md).

## Auto-Configuration

When Envoy starts, it:

1. Creates a `.mcp.json` file in your git repo root (auto-detected)
2. Generates a full [Claude Code configuration](claude-code.md) — rules, skills, slash commands, and project context
3. Auto-manages `.gitignore` entries for generated files

## Key Features

- **Zero setup** — Embody auto-installs all dependencies (`mcp`, `uvicorn`, etc.) when Envoy is first enabled
- **Auto-restarts** on port change
- **Localhost-only** binding (127.0.0.1) for security
- **Piggybacked logs** — every MCP response includes recent log entries
- **30-second timeout** per operation to prevent hangs
