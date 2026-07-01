# Claude Code Integration

When Envoy starts, it generates a complete Claude Code configuration in your project root. This gives Claude Code deep context about TouchDesigner development patterns, your project structure, and the MCP tools available through Envoy.

## Generated Files

| File/Directory | Purpose | Regenerated on start? |
|---|---|---|
| `CLAUDE.md` | Project context and critical rules | Yes |
| `.mcp.json` | MCP server connection config | Yes |
| `.embody/envoy-bridge.py` | STDIO-to-HTTP bridge for MCP transport | Yes |
| `.claude/settings.local.json` | Tool permissions and MCP server config | Yes |
| `.claude/rules/` | Always-loaded conventions (see below) | Yes (unless edited) |
| `.claude/skills/` | On-demand workflow guides (see below) | Yes (unless edited) |

All generated files except `CLAUDE.md` are automatically added to `.gitignore`.

The `Yes (unless edited)` files are refreshed from their templates on start **only while pristine**. Embody records a content hash of every file it generates (in `.embody/generated-hashes.json`); once you edit a generated rule or skill, your version is preserved and not overwritten — delete the file to opt back into regeneration. See [How It Works](#how-it-works).

## Rules (Always-Loaded)

Rules are loaded into every Claude Code conversation automatically. They provide conventions that prevent common mistakes when working with TouchDesigner.

| Rule | What it covers |
|------|----------------|
| `network-layout.md` | Grid spacing (200-unit grid), signal flow direction, annotation placement, operator positioning |
| `td-python.md` | Parameter access (`.eval()` vs `.val`), operator path portability, threading, cook model |
| `mcp-safety.md` | Thread boundary (never access TD from background thread), localhost binding, 30s timeout |
| `parameters.md` | Custom parameter design: value access, required help text, section breaks, ordering, pages, naming, and styles |
| `performance.md` | Performance gating protocol, stop conditions, crash/freeze avoidance, and safe-default resolution/feedback/GLSL caps |
| `td-connectivity.md` | Session-start connectivity checks, the bridge reconciler and Envoy liveness watchdog, and manual recovery steps |

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
| `/visual-aesthetics` | Before building or refining any rendered visual output (generative art, VJ visuals, shaders, scenes, renders) |

Each skill contains step-by-step workflows, API details, and common pitfalls specific to that operation.

## STDIO Bridge

Claude Code connects to Envoy through a STDIO bridge script (`.embody/envoy-bridge.py`) that translates between Claude Code's STDIO transport and Envoy's HTTP endpoint. The bridge provides four meta-tools that work even when TouchDesigner is not running:

| Tool | Description |
|------|-------------|
| `get_td_status` | Check if TD is running, whether Envoy is reachable, crash detection, restart attempts remaining, and instance registry status |
| `launch_td` | Launch TD with the project's `.toe` file and wait for Envoy to become reachable. On fresh clones (where `.embody/envoy.json`'s `td_executable` path doesn't exist locally), the bridge reads `td_build` from the committed `.embody/project.json` and auto-picks the matching TouchDesigner install — see [Architecture](architecture.md#embodyprojectjson-build-pin-committed). |
| `restart_td` | Gracefully quit TD, then relaunch and wait for Envoy |
| `switch_instance` | List all registered TD instances or switch the bridge to a different running instance |

This means Claude Code can start a TD session from scratch — no need to manually open TouchDesigner first. If TD crashes, Claude can detect it and restart automatically.

The bridge also handles crash-loop protection (max 3 launches in 5 minutes), automatic retry with backoff on transient connection failures, and orphan process cleanup when Claude Code exits.

### Working with Multiple Instances

If you have multiple TouchDesigner instances running with Envoy enabled (e.g., your main project and a test project), the bridge connects to one at a time. Use `switch_instance` to move between them:

- **List instances**: Call `switch_instance` with no arguments to see all registered instances and their reachability
- **Switch**: Call `switch_instance` with the instance name (`.toe` filename without the extension) to redirect all subsequent MCP calls to that instance

Each instance gets its own port automatically (ports 9870–9879). Switching is instant — no restart required.

!!! tip "Same-file instances"
    Opening the same `.toe` file in multiple TD instances works — Envoy auto-suffixes the registry key (`MyProject`, `MyProject-2`, etc.).

See [Architecture](architecture.md#multiple-instances) for technical details.

## How It Works

Embody stores master copies of all rules and skills as template DATs inside the `templates` baseCOMP. When Envoy starts in a user project, `_extractClaudeConfig()` reads these templates and writes them to the project's `.claude/` directory. This means:

- **Updates are automatic** — upgrading Embody gives you the latest rules and skills
- **Templates are the source of truth for pristine files** — an unedited generated `.claude/` file is refreshed from its template on Envoy start, but Embody records a content hash of each file it generates (`.embody/generated-hashes.json`) and will not overwrite one you've since edited (delete the file to opt back into regeneration)
- **Project-specific customization** — add your own rules or skills to `.claude/` alongside the generated ones (they won't be overwritten)

## Live Build Visualization

Turn on **Envoy Follow** (the `Envoyfollow` toggle on the Embody COMP's Envoy page, OFF by default) to *watch* Claude build in real time. While the agent works through Envoy:

- **Within the network you're viewing**, the editor smoothly **glides** to center on each operator just touched (ease-out, one step per frame).
- **When the work moves to a COMP no pane is showing**, the editor **navigates** a network-editor pane into that COMP and snaps to frame the op — you can't glide across networks (different coordinate spaces), so it cuts.
- A small **builder-bot** ("embot") — a figure made of minimal network-box annotations — **hops between the nodes** being worked on, hovers when idle, and throws an occasional gesture (a wave, a reach, a pump, the odd robot dance). Its color tracks "thinking time": cool cyan-green right after Envoy acts, warming toward red the longer the gap. The node Envoy just touched pulses the Envoy accent.

It **yields the instant you pan, zoom, or navigate** the view yourself, and resumes only once you stop — it never yanks the view mid-interaction. The bot and pulse retire after a stretch of quiet.

This is purely a viewing aid: it writes only pane/view state (which TouchDesigner never externalizes), the bot is destroyed before every save, and it runs entirely on the main thread, so it adds nothing to your saved files and never affects a build. Leave it off if you'd rather your view never move on its own.

## Customization

You can extend the generated configuration:

- **Add project-specific rules**: Create additional `.md` files in `.claude/rules/` — Claude Code loads all rules in this directory
- **Add custom commands**: Create `.md` files in `.claude/commands/` with prompt instructions
- **Modify permissions**: Edit `.claude/settings.local.json` to allow or restrict specific tools

!!! tip
    You can edit the Envoy-generated rules and skills directly — Embody records a content hash of each generated file (`.embody/generated-hashes.json`) and won't overwrite one you've changed (it logs that it kept your edits). Pristine generated files still refresh on regeneration; delete a file to discard your changes and pull the latest template.
