# Claude Code Integration

When Envoy starts, it generates a complete Claude Code configuration in your project root. This gives Claude Code deep context about TouchDesigner development patterns, your project structure, and the MCP tools available through Envoy.

## Generated Files

| File/Directory | Purpose | Regenerated on start? |
|---|---|---|
| `CLAUDE.md` | Project context and critical rules | Yes |
| `.mcp.json` | MCP server connection config | Yes |
| `.claude/envoy-bridge.py` | STDIO-to-HTTP bridge for MCP transport | Yes |
| `.claude/settings.local.json` | Tool permissions and MCP server config | Yes |
| `.claude/rules/` | Always-loaded conventions (see below) | Yes |
| `.claude/skills/` | On-demand workflow guides (see below) | Yes |

All generated files except `CLAUDE.md` are automatically added to `.gitignore`.

## Rules (Always-Loaded)

Rules are loaded into every Claude Code conversation automatically. They provide conventions that prevent common mistakes when working with TouchDesigner.

| Rule | What it covers |
|------|----------------|
| `network-layout.md` | Grid spacing (200-unit grid), signal flow direction, annotation placement, operator positioning |
| `td-python.md` | Parameter access (`.eval()` vs `.val`), operator path portability, threading, cook model |
| `mcp-safety.md` | Thread boundary (never access TD from background thread), localhost binding, 30s timeout |
| `skill-prerequisites.md` | Which skills must be loaded before calling specific MCP tools |

## Skills (On-Demand)

Skills are loaded only when needed, keeping the context window lean. Claude Code loads them automatically before performing the relevant operation.

| Skill | Trigger |
|-------|---------|
| `/create-operator` | Before creating operators via `create_op` |
| `/create-extension` | Before creating TD extensions via `create_extension` |
| `/debug-operator` | When diagnosing operator errors |
| `/externalize-operator` | Before tagging or saving externalizations |
| `/manage-annotations` | Before creating or modifying annotations |
| `/td-api-reference` | Before writing TD Python code |
| `/mcp-tools-reference` | Before the first MCP call in a session |

Each skill contains step-by-step workflows, API details, and common pitfalls specific to that operation.

## Slash Commands

Slash commands are shortcuts you can type directly in Claude Code to trigger common workflows.

### `/run-tests`

Runs the Embody test suite via MCP and reports results.

```
/run-tests                              # Run all 30 test suites
/run-tests test_path_utils              # Run a specific suite
/run-tests test_path_utils test_name    # Run a specific test
```

Reports pass/fail counts per suite. On failure, automatically reads log files for full error context.

### `/status`

Performs a quick health check of the Embody project:

- Confirms Envoy is connected (TD version, Envoy status)
- Reports any dirty (unsaved) externalizations
- Scans for operator errors in the network
- Checks recent log entries for errors or warnings

### `/explore-network`

Discovers and reports the structure of a TouchDesigner network:

```
/explore-network                        # Explore the current network
/explore-network /project1/base1        # Explore a specific path
```

Returns operators organized by annotation groups, signal flow direction, and any errors found.

## STDIO Bridge

Claude Code connects to Envoy through a STDIO bridge script (`.claude/envoy-bridge.py`) that translates between Claude Code's STDIO transport and Envoy's HTTP endpoint. The bridge provides three meta-tools that work even when TouchDesigner is not running:

| Tool | Description |
|------|-------------|
| `get_td_status` | Check if TD is running, whether Envoy is reachable, crash detection, and restart attempts remaining |
| `launch_td` | Launch TD with the project's `.toe` file and wait for Envoy to become reachable |
| `restart_td` | Gracefully quit TD, then relaunch and wait for Envoy |

This means Claude Code can start a TD session from scratch — no need to manually open TouchDesigner first. If TD crashes, Claude can detect it and restart automatically.

The bridge also handles crash-loop protection (max 3 launches in 5 minutes), automatic retry with backoff on transient connection failures, and orphan process cleanup when Claude Code exits.

See [Architecture](architecture.md) for technical details on the bridge's design.

## How It Works

Embody stores master copies of all rules and skills as template DATs inside the `templates` baseCOMP. When Envoy starts in a user project, `_extractClaudeConfig()` reads these templates and writes them to the project's `.claude/` directory. This means:

- **Updates are automatic** — upgrading Embody gives you the latest rules and skills
- **Templates are the source of truth** — the generated `.claude/` files are overwritten on each Envoy start
- **Project-specific customization** — add your own rules or skills to `.claude/` alongside the generated ones (they won't be overwritten)

## Customization

You can extend the generated configuration:

- **Add project-specific rules**: Create additional `.md` files in `.claude/rules/` — Claude Code loads all rules in this directory
- **Add custom commands**: Create `.md` files in `.claude/commands/` with prompt instructions
- **Modify permissions**: Edit `.claude/settings.local.json` to allow or restrict specific tools

!!! warning
    Don't modify the Envoy-generated rules or skills — they'll be overwritten when Envoy restarts. Add your own files alongside them instead.
