# Configuration

## Embody Parameters

Embody is configured through parameters on the Embody COMP itself. Key parameters include:

### Embody

- **Setup Wizard** — Pulse to re-open the [Setup Wizard](setup-wizard.md) and review or change your mode, AI assistant, tool permissions, and config-file location. Nothing is applied until you confirm the summary screen.
- **Mode** — How Embody handles invasive, project-level changes (git config, `.venv`, MCP/AI config files, network ops). *Auto* (default) manages everything silently — recommended for most users. *Advanced* asks before each such change and keeps the footprint minimal.
- **Externalizations Folder** — The externalization folder path (static or expression mode)
- **Disable** — Disable Embody (stops all externalization tracking)
- **Enable/Update** — Initialize or update all externalizations
- **Externalize Full Project** — Pulse to externalize every eligible operator in the project
- **Detect Duplicate Paths** — Enable/disable duplicate path detection prompts
- **Dropped .tox Expression** — How Embody treats the default expression TouchDesigner auto-writes into a COMP's External .tox when a `.tox` is dragged into the network (`me.parent().fileFolder + '/' + ...`). `Ask` (default) prompts when the continuity sweep detects them, listing the affected COMPs (truncated for very large sets so the buttons stay reachable) with four choices — **Clean** (clear the expression once), **Ignore** (leave it once), **Always Clean**, and **Always Ignore**. The two *Always* buttons persist the choice into this parameter so you are not re-prompted. Embody's own descendant COMPs are always cleaned regardless of this setting.
- **Template Master Name** — When a duplicate-path group has exactly one operator whose path contains this name, that operator is auto-selected as the master (default `__template__`). Clear it to always choose the master manually, or set your own convention (e.g. `_master`). See [Duplicate Path Handling](externalization.md#duplicate-path-handling).
- **Open Manager** / **Close Manager** — Toggle the Manager UI
- **Clipboard Auto-Paste** — When ON (default), Embody watches the OS clipboard (~1.5s poll) for an **inbound** TDN network — e.g. from the web "embody it" button — and prompts to paste it as a new COMP in the current network. Your own **outbound** copies (++ctrl+shift+c++ on a COMP, which exports it to the clipboard to share or paste elsewhere) are recognized and do **not** re-trigger the prompt. Disabled in Perform Mode and self-suppressed during saves and tests. Turn OFF to ignore the clipboard. See [Keyboard Shortcuts](keyboard-shortcuts.md).
- **Uncommitted Color** — RGB color of the manager's **orange** git-uncommitted badge (an externalized file saved to disk but not yet committed to git). Defaults to a warm orange. See [Manager UI — Status Indicators](manager-ui.md#status-indicators).

### Envoy

- **Envoy Enable** — Toggle the MCP server on/off
- **Envoy Port** — Port number for the MCP server (default: 9870)
- **AI Client** — Which AI coding assistant to generate config for (`Claude Code`, `Codex`, `Gemini`, `Cursor`, `Windsurf`, `GitHub Copilot`, or `None`). Switching clients regenerates the corresponding files. `AGENTS.md` is always written; `Codex` reads that alone, `Gemini` adds a thin `GEMINI.md` that imports it, and the others add their own client-specific files.
- **Launch AI Client** — Pulse to open the assistant selected in **AI Client** at the project root (`_findProjectRoot()`, honoring **AI Project Root**). Editors (Cursor, Windsurf; GitHub Copilot opens VS Code) open the root folder as a workspace; terminal CLIs (Claude Code, Codex, Gemini) open in a new terminal there. Fire-and-forget — it opens a window and does not confirm the tool actually ran; a missing tool logs a per-client install hint instead.
- **AI Project Root** — Where Embody writes AI/MCP config files (`AGENTS.md`, `CLAUDE.md`, `.claude/`, `.cursor/`, `.github/copilot-instructions.md` + `.github/instructions/`, `.windsurf/rules/`, `GEMINI.md`, `.mcp.json`, `.embody/` — which of the client-specific files appear depends on **AI Client**). Three modes:
    - *Git root* (default) — config lives at the top of the git repository. This is the right choice when the whole repo is your AI tool's workspace.
    - *Project folder (.toe directory)* — config lives next to the `.toe`. Use this when your TouchDesigner project lives in a subdirectory of a larger repo and you open that subdirectory as your AI tool's workspace (e.g. `myrepo/touchdesigner/` opened in Cursor or Claude Code).
    - *Custom* — config lives at a directory you pick via the **AI Project Root (Custom)** parameter (paired Folder picker, greyed out when this mode isn't selected). Useful for monorepos with multiple `.toe` files that share a parent directory — set the same relative path (e.g. `../`) on each `.toe` and they all converge on one set of AI config files plus a shared `.embody/envoy.json`, which lets the multi-instance MCP feature work naturally across sibling projects.
    Flipping the parameter (or changing the custom path while in Custom mode) migrates Embody's own state (`.embody/config.json`, `project.json`, palette catalogs, `.claude/settings.local.json`) to the new root and cleans up Embody-generated AI files at the old root. User-authored files (custom skills, hand-edited `CLAUDE.md`, other entries in `.mcp.json`) are preserved.
- **AI Project Root (Custom)** — Custom directory for AI/MCP config when **AI Project Root** is set to `Custom`. Relative paths are resolved against the `.toe` directory (e.g. `../` places config one level above the `.toe`); absolute paths are used as-is. Greyed out unless the menu is on `Custom`.
- **Embot** — When ON (default), a small builder-bot mascot ("embot") appears on each operator Envoy creates or edits and narrates what it does. Independent of the camera — turn OFF to hide the character entirely (no mascot, no narration). See [Claude Code Integration — Live Build Visualization](../envoy/claude-code.md#live-build-visualization).
- **Envoy Follow** — When ON (default), the network editor follows Envoy's work as Claude builds — gliding to center on each operator just touched, and navigating into whatever COMP is being built when the work moves to a network you're not viewing. Works with or without the Embot character shown. Yields the instant you pan, zoom, or navigate the view yourself, and resumes once you stop. View-only (writes pane state, never externalized). See [Claude Code Integration — Live Build Visualization](../envoy/claude-code.md#live-build-visualization).
- **Auto-Externalize New Ops** — **Opt-in**, default `Neither` (off). When set to `DATs`, `COMPs`, or `DATs and COMPs`, operators the AI creates through Envoy's `create_op` are automatically tagged and externalized to version-control-friendly files (COMPs as `.tdn`, DATs as source files). With the default `Neither`, nothing the AI builds is written to disk unless you tag it yourself. Boundary-scoped and additive only — it never tags ops inside an already-externalized COMP, and never deletes files or removes tags.
- **Tool Permissions** — How much Embody pre-approves Envoy MCP tool calls in Claude Code's `.claude/settings.local.json` so you aren't prompted on every call: `Don't ask (all)` approves every Envoy tool, `Ask for some` approves only read-only tools, `Ask for all` pre-approves nothing, `Leave settings alone` never touches the file. Claude Code only. See [Parameters — Envoy](parameters.md#envoy).

### Restoration

- **TOX Restore on Start** — Restore missing TOX-strategy COMPs from `.tox` files on project open (ON by default)
- **DAT Restore on Start** — Restore missing DAT-strategy operators from their externalized source files on project open, recreating the DAT and re-wiring it for auto-sync (ON by default)
- **TDN Create on Start** — Reconstruct TDN-strategy COMPs from `.tdn` files on project open. **Only active when `TDN Mode` = Roundtrip.** In Export-on-Save mode, the `.toe` is the source of truth and reconstruction is skipped. See [TDN](#tdn) for the full parameter listing.

### TDN

- **TDN Mode** — Master three-way switch for the TDN subsystem:
    - *Off* — no TDN runtime. `.tdn` files on disk are preserved; Embody stops touching them.
    - *Export-on-Save* — **default**. Writes `.tdn` files on save. `.toe` is the source of truth; live network is never stripped. Ideal for git-diff and MCP workflows.
    - *Roundtrip (Experimental)* — bidirectional strip/restore. Children are stripped from the `.toe` on save and rebuilt from `.tdn` on open. May hit edge cases with extension reload timing on deeply-nested TDN COMPs.
    - **Upgrading from the old `Tdnenable` toggle**: on first project open after upgrade, Embody detects the legacy parameter in `.embody/config.json` and shows a one-shot dialog offering Export-on-Save (recommended) or a one-click restore of the previous Full behavior via Roundtrip mode. Your existing `.tdn` files and tracked COMP entries are preserved across the switch; the nudge fires once per project and never again.
- **Cascade to Children** — When tagging a COMP for TDN, automatically tag all child COMPs so each gets its own `.tdn` file
- **Large TDN Warning** — *Ask* (default) prompts when a `.tdn` file exceeds 5 MB, *Quiet* suppresses the warning
- **Embed DATs in TDNs** — Include DAT content in TDN exports
- **Embed Storage in TDNs** — Include Python storage entries in TDN exports (can be overridden per-COMP from the tagging menu)
- **TDN Create on Start** — Reconstruct TDN-strategy COMPs from `.tdn` files on project open (Roundtrip mode only; greyed in Off/Export)
- **Strip on Save** — Strip children from TDN-strategy COMPs on save (Roundtrip mode only; greyed in Off/Export — Export-on-Save never strips)
- **Palette Handling** — How to handle TD palette COMPs (e.g. `abletonLink`, Widget components) during TDN export. *Ask* (default) prompts on first encounter per COMP with four choices; *Black Box* always references the palette and skips internal children (correct for stock palette COMPs); *Full Export* always exports all internals (for heavily customized palette COMPs). Native operator templates (`/sys/TDTox/defaultCOMPs/`) are excluded from palette detection — a plain `buttonCOMP` or `panelCOMP` is treated as a regular COMP, not a palette clone. See [TDN Palette Handling](../tdn/specification.md#palette-handling) for details
- **Content Safety** — What to do when TDN COMPs contain DATs or storage that would be lost on save: *Ask Each Save* (default) prompts before each save, *Always Externalize* auto-externalizes at-risk DATs without asking, *Never Ask* suppresses the check entirely. In *Ask Each Save* mode the save-time dialog offers four buttons — **Externalize DATs** (externalize the at-risk DATs now), **Always Externalize** (do that and set this parameter to *Always Externalize*), **Skip Once** (proceed this save only), and **Always Skip** (proceed and set this parameter to *Never Ask*, disabling future warnings). The *Never Ask* value is an opt-in escape hatch for power users.
- **Export Project TDN** — Pulse to export the entire project network
- **Auto-Save Checkpoints** — When ON (default), a beat after the agent (or you) goes idle Embody writes any changed TDN COMP to disk as a frame-cheap `.tdn` checkpoint (~3-6 ms, no full project save, no strip/restore, no freeze) so an accidental crash loses little unsaved work — the checkpointed COMPs rebuild on next open. Also fires a synchronous pre-checkpoint before a destructive `delete_op` inside a tracked COMP. Bypassed in Perform Mode and during saves, and perf-gated so it never piles onto a hot frame. `execute_python` is deliberately not a trigger. See [Crash Recovery](externalization.md#crash-recovery).
- **Auto-Save Status** — Read-only readout of the auto-save engine's state: *Idle* / *Saved &lt;time&gt;* / *Bypassed* (Perform Mode) / *Disabled*.

### Shortcuts

Every Embody keyboard shortcut is remappable here — type a combo or pulse
**Record** and press the keys; empty disables a binding. Includes the
Tagger Double-Tap Key menu and a reset pulse. Full details:
[Keyboard Shortcuts](keyboard-shortcuts.md#customizing-shortcuts).

### Logs

- **Verbose (Debug)** — Enable debug-level logging
- **Print to Textport** — Echo logs to the textport
- **Log to File** — Enabled by default, writes to `logs/<project_name>_YYMMDD.log`

## Settings Persistence

Embody automatically saves your parameter settings to a `config.json` file in the `.embody/` folder so they survive upgrades, crashes, and force-quits.

- **Location**: `.embody/` folder at the AI Project Root (next to `.mcp.json`) — the git root by default, or the project folder / a custom directory per the **AI Project Root** parameter (see [Envoy](#envoy) above)
- **When saved**: Automatically on every parameter change (debounced to 1 frame)
- **When restored**: On every project open (frame 5), and on fresh install after dropping in a new `.tox`
- **What's saved**: Folder path, Envoy config, tag names, tag colors, TDN settings, logging options, keyboard shortcut bindings (the Shortcuts page, including the tagger double-tap key and the master toggle), and other user-configurable parameters. Read-only status fields and runtime state are excluded

The file is created on your first parameter change — no `.embody/config.json` exists until you customize something. If the file is missing or corrupt, Embody uses its built-in defaults.

!!! tip "Upgrading Embody"
    When you drop a new Embody `.tox` into your project, your saved settings are automatically restored. No manual reconfiguration needed.

## Logging System

Embody provides a multi-destination logging system:

- **File logging** (default): Logs are written to `logs/<project_name>_YYMMDD.log`. Files auto-rotate at 10 MB with numbered suffixes (`_001`, `_002`, etc.).
- **FIFO DAT**: Recent log entries are visible in TouchDesigner's network editor.
- **Textport**: Enable the **Print to Textport** parameter to echo logs to the textport.
- **Ring buffer**: The most recent 200 entries are accessible via the Envoy `get_logs` MCP tool.

### Log Levels

`DEBUG`, `INFO`, `WARNING`, `ERROR`, `SUCCESS`

### Using the Logger

From anywhere in your project:

```python
op.Embody.Log('Something happened', 'INFO')
op.Embody.Debug('Debug message')
op.Embody.Info('Informational message')
op.Embody.Warn('Warning message')
op.Embody.Error('Error message')
```
