# Envoy MCP Server

**Envoy** is the forward velocity layer of the project. It's an embedded [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) server that lets your AI assistant build directly inside your live TouchDesigner session. Say what you want — operators, connections, parameters, extensions, fixes, even tests — and watch it happen on the screen in front of you. The distance between an idea and a working network collapses to the time it takes to type a sentence.

## Compatible Clients

Envoy works with any MCP client, including:

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI and VS Code extension)
- [Cursor](https://www.cursor.com/)
- [Windsurf](https://windsurf.com/)
- Any other client that supports the MCP protocol

## Capabilities

Envoy exposes **46 MCP tools** organized into categories:

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
| **Batch Operations** | Combine multiple tool calls into a single request |

For the complete tool reference, see [Tools Reference](tools-reference.md).

## Auto-Configuration

When Envoy starts, it:

1. Creates `.mcp.json` and MCP bridge files in your git repo root or project folder
2. Generates a full [Claude Code configuration](claude-code.md) — rules, skills, slash commands, and project context
3. Auto-manages `.gitignore` and `.gitattributes` entries (when a git repo is present)

You can regenerate these files at any time with `op.Embody.InitEnvoy()` (MCP + AI config) or `op.Embody.InitGit()` (git config + re-run InitEnvoy). See [Setup](setup.md#regenerating-config-files) for details.

## Key Features

- **Zero setup** — Embody auto-installs all dependencies (`mcp`, `uvicorn`, etc.) when Envoy is first enabled
- **Auto-restarts** on port change or crash (exponential backoff, up to 3 attempts)
- **Localhost-only** binding (127.0.0.1) for security
- **Piggybacked logs** — every MCP response includes recent log entries
- **30-second timeout** per operation to prevent hangs
