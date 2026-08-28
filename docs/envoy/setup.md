# Envoy Setup

## Prerequisites

You'll need:

- **TouchDesigner 2025.33070** or later
- An MCP-compatible client such as [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [OpenCode](https://opencode.ai/), [Codex](https://github.com/openai/codex), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Cursor](https://www.cursor.com/), [Windsurf](https://windsurf.com/), or GitHub Copilot via VS Code

Embody automatically installs all server-side dependencies (`mcp`, `uvicorn`, etc.) when Envoy is first enabled — no manual Python setup required. This first install (and any later dependency upgrade) runs **in a background thread** so TouchDesigner stays responsive; the Embody COMP shows `Installing deps... (one-time)` while it works and switches to `Running on port …` once MCP is ready. After that, every startup takes the fast path and skips the install entirely. Envoy validates the virtual environment on each startup and falls back to the system Python if the venv is broken (see [Broken Virtual Environment](troubleshooting.md#broken-virtual-environment)). The environment is shared with your own code too — any DAT or extension in the project can import from it, and your own packages can live in it: see [Python Environment](../embody/python-environment.md).

## Enabling Envoy

1. **Enable Envoy**: Toggle the **Envoy Enable** parameter on the Embody COMP
2. **Server starts**: Envoy runs on `127.0.0.1:9870` (configurable via **Envoy Port**)
3. **Auto-configuration**: Envoy creates `.mcp.json` and AI client config files at the root chosen by the **AI Project Root** parameter — the git repo root by default (`gitroot`), or the `.toe`'s own folder (`projectfolder`), or a custom path. When `gitroot` is selected but no git repo exists, Envoy falls back to the project folder and still writes the config. If your project is in a git repo, Envoy also generates `.gitignore` and `.gitattributes` entries.
4. **Connect your MCP client**: Start a new Claude Code session (or restart your IDE) — it picks up the `.mcp.json` automatically

## Regenerating Config Files

You can regenerate Envoy's config files at any time from the TD textport or a script:

```python
op.Embody.InitEnvoy()   # Regenerate MCP + AI client config
op.Embody.InitGit()     # Init/reconnect git repo + .gitignore/.gitattributes
```

Use `InitEnvoy()` after updating Embody, changing which clients you configure for, or if config files were accidentally deleted. Use `InitGit()` after creating a git repo manually, or to refresh `.gitignore`/`.gitattributes` entries. `InitGit()` also calls `InitEnvoy()` to update paths.

## Manual Configuration

If you prefer manual control, create `.mcp.json` in your project directory. You can use either the direct HTTP transport or the STDIO bridge:

**HTTP transport** (simpler, requires TD to be running):

```json
{
  "mcpServers": {
    "envoy": {
      "type": "http",
      "url": "http://127.0.0.1:9870/mcp"
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

The STDIO bridge provides meta-tools (`get_td_status`, `launch_td`, `restart_td`, `switch_instance`) that work even when TouchDesigner is not running. See [Claude Code Integration](claude-code.md#stdio-bridge) for details.

### Every client reads a different file

`.mcp.json` is Claude Code's format, and **no other client reads it** — VS Code
does not even use the same root key. So Embody writes each selected client's own
MCP config as well, every one of them spawning the *same* STDIO bridge, so every
client gets the bridge's resilience layer (meta-tools while TD is down,
reconnection, instance-registry identity checks) rather than a bare URL.

| Client | MCP config file | Root key |
|---|---|---|
| Claude Code | `.mcp.json` | `mcpServers` |
| OpenCode | `opencode.json` | `mcp` |
| Codex | `.codex/config.toml` | `[mcp_servers.envoy]` |
| Gemini | `.gemini/settings.json` | `mcpServers` |
| VS Code | `.vscode/mcp.json` | `servers` |
| GitHub Copilot | `.vscode/mcp.json` (shared with VS Code) | `servers` |
| Cursor | `.cursor/mcp.json` | `mcpServers` |
| Antigravity | `.agents/mcp_config.json` | `mcpServers` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |

Merging is conservative in every case: an existing file keeps all of its other
servers and unrelated settings, only the `envoy` entry is written, a second
deploy with an unchanged bridge command rewrites nothing, and a file Embody
cannot parse (VS Code and Cursor both accept JSONC, which is not valid JSON) is
left **completely untouched** with a warning rather than clobbered.

Two clients need a manual step:

- **Codex** reads project-level config only for projects you have trusted. Run
  `codex` once in the project folder and trust it, or the `.codex/config.toml`
  Embody writes is ignored.
- **Windsurf** has no project-level MCP config at all — Cascade reads only the
  user-global `~/.codeium/windsurf/mcp_config.json`, shared by every project you
  open. Embody will **not** write a global file that points all of your projects
  at this one's bridge, so add the `envoy` entry there by hand once. Everything
  else for Windsurf (`.windsurf/rules/`) is generated normally.

### Files you already own

Embody never overwrites a file you wrote. Which of two things it does depends
on the file:

- **`AGENTS.md`, `GEMINI.md`, `ENVOY.md` and `.github/copilot-instructions.md`
  are merged into.** Your content is left exactly as it is and Embody
  maintains one delimited block inside the file, refreshed in place on later
  deploys. Uninstall removes only that block and leaves the file.
- **Per-rule and per-skill files are skipped.** `.claude/rules/td-python.md`,
  `.cursor/rules/*.mdc` and their siblings have Embody-specific names, so a
  file of yours at one of those paths is left completely alone -- the other
  rules are still written around it.

A file Embody cannot read as UTF-8 is skipped with a warning rather than
rewritten, and it never stops the rest of the deploy.

### Which files each client gets

`AGENTS.md` (the universal standard read by all major AI tools) and `.mcp.json`
are written whenever any client config is generated -- that is, unless
**Configure For** is `None`.

| Configure For | Also writes |
|---|---|
| Claude Code | `CLAUDE.md` (or `ENVOY.md` if you already have your own), `.claude/rules/`, `.claude/skills/` |
| OpenCode | `opencode.json`, and shares Claude Code's `.claude/rules/` + `.claude/skills/` |
| Codex | `.codex/config.toml` (Codex reads `AGENTS.md` natively, so it needs no rules files of its own) |
| Gemini | `GEMINI.md` (a thin `@AGENTS.md` import), `.gemini/settings.json` |
| VS Code | `.vscode/mcp.json` |
| GitHub Copilot | `.github/copilot-instructions.md`, `.github/instructions/`, and `.vscode/mcp.json` (shared with VS Code) |
| Cursor | `.cursor/rules/*.mdc`, `.cursor/mcp.json` |
| Windsurf | `.windsurf/rules/` only — its MCP config is user-global and is never written, see above |
| Antigravity | `.agents/rules/`, `.agents/skills/`, `.agents/mcp_config.json` |

Serving more than one client is normal and supported: select each in turn.
Generation is additive and never removes, so an earlier client stays configured
and its MCP config keeps tracking bridge and port changes. See
[Local Models & Open Clients](local-models.md) for OpenCode's full setup,
including local-model recommendations.

## Changing the Port

Change the **Envoy Port** parameter on the Embody COMP. If the server is running, it automatically:

1. Stops the server on the old port
2. Restarts on the new port (after a 2-frame delay for clean shutdown)
3. Updates `.mcp.json` with the new port

If the server is not running, changing the port simply updates the parameter value.

## Running Multiple Instances

You can run multiple TouchDesigner instances with Envoy enabled in the same git repo. Each instance automatically claims a unique port from the range `[base_port, base_port + 9]` (default: 9870–9879).

To switch between instances from Claude Code, use the `switch_instance` bridge meta-tool. See [Claude Code Integration](claude-code.md#working-with-multiple-instances) for usage details and [Architecture](architecture.md#multiple-instances) for how it works.

## Claude Code Integration

When Envoy starts, it generates a full Claude Code configuration in your project root:

- **`AGENTS.md`** — universal AI instructions, always written regardless of which clients are selected. If your repo already has its own `AGENTS.md`, Embody **merges** rather than replacing it: your content is left alone and Embody keeps a single delimited `<!-- BEGIN Embody/Envoy -->` block inside the file, updated in place on later deploys. Uninstall removes just that block
- **`CLAUDE.md`** — project context and critical rules
- **`.claude/rules/`** — always-loaded conventions (TD Python, network layout, MCP safety)
- **`.claude/skills/`** — on-demand workflow guides (operator creation, debugging, externalization)

Pristine generated files are refreshed each time Envoy starts to stay up to date. If you edit a generated rule or skill, Embody detects the change — Embody stamps each file it generates with a hash of its own content, in the `<!-- Generated by Embody/Envoy ... sha:... -->` marker line — and keeps your version instead of overwriting it; delete the file to opt back into regeneration. The stamp is part of the file, so an edit you commit is still recognised on every other machine that clones the repo. See [Claude Code Integration](claude-code.md) for the full reference.

## MCP Tool Permissions

By default, Claude Code asks for confirmation every time it wants to use an MCP tool. When you turn on the AI assistant in the [setup wizard](../embody/setup-wizard.md) (Claude Code only), a **"How should the AI ask permission?"** step lets you choose how much Embody pre-approves in `.claude/settings.local.json` — in both Auto and Advanced modes:

| Choice | Effect |
|---|---|
| **Don't ask** (recommended) | Auto-approves **all** Envoy tools via the `mcp__envoy` wildcard — no prompts, and new tools are covered automatically. |
| **Ask for some** | Auto-approves only read-only/query tools (`get_*`, `query_network`, `read_tdn`, `capture_top`, …). Anything that creates, edits, deletes, or executes still prompts. |
| **Ask for all** | Pre-approves nothing — Claude Code prompts before every tool (the built-in default behavior). |
| **Leave settings alone** | Embody does not create or modify `settings.local.json` at all — you manage permissions yourself. |

The choice is stored on the **Tool Permissions** (`Toolpermissions`) parameter on Embody's Envoy page, so you can change it anytime without re-running the wizard.

Every written posture (all but *Leave*) also whitelists your operating system's temp directory in `additionalDirectories`, so a TOP captured with `capture_top` (saved to the temp dir) can be read back without a permission prompt.

Every written posture also pre-authorizes **sibling worktree folders**: Read/Edit rules for the `<your-repo>-wt-*` pattern beside your project root, computed at runtime from wherever your repo actually lives (any drive, any OS). The AI worktree workflow (`git worktree add ../<repo>-wt-<task>` — see the generated `worktree-td-safety` rule) creates folders outside the workspace, which would otherwise trigger a permission prompt for every file the AI touches there. Settings written by older Embody versions gain these rules automatically the next time Envoy starts.

Envoy also **mirrors the AI config into existing worktrees**: `.mcp.json` and `.claude/settings.local.json` are gitignored, so a fresh worktree checkout has neither — an AI session launched *inside* a worktree would find no Envoy MCP server and prompt for every tool. On each config deploy — and on every registry refresh (each project save) — Envoy copies both files into every sibling `<your-repo>-wt-*` worktree (identified by name pattern plus a `.git` entry), skipping files that are already up to date and leaving worktree-native sandboxes (a worktree with its own `.embody/envoy.json`) untouched. A worktree created mid-session picks up config at the next save; to hand it config immediately, copy the two files from the repo root. See [Git Worktrees](worktrees.md) for the full worktree workflow.

**Existing files are preserved.** If a `.claude/settings.local.json` already exists, Embody updates only its Envoy tool entries and keeps everything else you have set (hooks, model, other `allow` patterns) — and it only rewrites when the posture actually changes. Choose *Leave settings alone* to keep Embody entirely hands-off.

You can also edit `.claude/settings.local.json` directly at any time; the `allow` array lists tool-permission patterns. This file is gitignored.

## Fresh Clones and TD Version Matching

When you clone a repo someone else built with Embody, the `.embody/envoy.json` file (which records the local TD install path) is gitignored — the path it references won't exist on your machine. On a fresh clone the bridge simply scans your standard TouchDesigner install locations (`/Applications/TouchDesigner*.app` on macOS, `C:\Program Files\Derivative\TouchDesigner.*` on Windows, `/opt/derivative/touchdesigner-*` on Linux) and launches the newest install it finds. If nothing is installed, the error response includes the Derivative download link.

Once TD has run on your machine, Embody pins the build you used into the machine-local `.embody/local.json`, and later launches prefer the exact (or closest same-year) match for that pin — so your machine keeps launching the build **you** run, regardless of what your collaborators run. (Older repos may still carry a legacy `td_build` key in the committed `project.json`; it is tolerated as a fallback and removed automatically by newer Embody builds — a committed pin churned whenever collaborators ran different TD builds.) See [Architecture](architecture.md#embodylocaljson-build-pin-machine-local-and-embodyprojectjson-committed) for the full match policy.

## Verifying the Connection

After starting Envoy and your MCP client:

1. The Embody COMP should show **Envoy Enable** toggled on and a status indicator
2. Your MCP client should list the Envoy tools (e.g., `create_op`, `get_op`, `set_parameter`)
3. Try a simple command like "list all operators in the project" to verify the connection
