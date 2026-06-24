# Configuration

## Embody Parameters

Embody is configured through parameters on the Embody COMP itself. Key parameters include:

### Embody

- **Externalizations Folder** — The externalization folder path (static or expression mode)
- **Disable** — Disable Embody (stops all externalization tracking)
- **Enable/Update** — Initialize or update all externalizations
- **Externalize Full Project** — Pulse to externalize every eligible operator in the project
- **Detect Duplicate Paths** — Enable/disable duplicate path detection prompts
- **Template Master Name** — When a duplicate-path group has exactly one operator whose path contains this name, that operator is auto-selected as the master (default `__template__`). Clear it to always choose the master manually, or set your own convention (e.g. `_master`). See [Duplicate Path Handling](externalization.md#duplicate-path-handling).
- **Open Manager** / **Close Manager** — Toggle the Manager UI
- **Clipboard Auto-Paste** — When ON (default), Embody watches the OS clipboard (~1.5s poll) for a copied TDN network — from the web "embody it" button, or ++ctrl+shift+c++ on a COMP — and prompts to paste it as a new COMP in the current network. Disabled in Perform Mode and self-suppressed during saves and tests. Turn OFF to ignore the clipboard. See [Keyboard Shortcuts](keyboard-shortcuts.md).
- **Uncommitted Color** — RGB color of the manager's **orange** git-uncommitted badge (an externalized file saved to disk but not yet committed to git). Defaults to a warm orange. See [Manager UI — Status Indicators](manager-ui.md#status-indicators).

### Envoy

- **Envoy Enable** — Toggle the MCP server on/off
- **Envoy Port** — Port number for the MCP server (default: 9870)
- **AI Client** — Which AI coding assistant to generate config for (`Claude Code`, `Cursor`, `Copilot`, `Windsurf`, or `None`). Switching clients regenerates the corresponding files.
- **AI Project Root** — Where Embody writes AI/MCP config files (`AGENTS.md`, `CLAUDE.md`, `.claude/`, `.cursor/`, `.mcp.json`, `.embody/`). Three modes:
    - *Git root* (default) — config lives at the top of the git repository. This is the right choice when the whole repo is your AI tool's workspace.
    - *Project folder (.toe directory)* — config lives next to the `.toe`. Use this when your TouchDesigner project lives in a subdirectory of a larger repo and you open that subdirectory as your AI tool's workspace (e.g. `myrepo/touchdesigner/` opened in Cursor or Claude Code).
    - *Custom* — config lives at a directory you pick via the **AI Project Root (Custom)** parameter (paired Folder picker, greyed out when this mode isn't selected). Useful for monorepos with multiple `.toe` files that share a parent directory — set the same relative path (e.g. `../`) on each `.toe` and they all converge on one set of AI config files plus a shared `.embody/envoy.json`, which lets the multi-instance MCP feature work naturally across sibling projects.
    Flipping the parameter (or changing the custom path while in Custom mode) migrates Embody's own state (`.embody/config.json`, `project.json`, palette catalogs, `.claude/settings.local.json`) to the new root and cleans up Embody-generated AI files at the old root. User-authored files (custom skills, hand-edited `CLAUDE.md`, other entries in `.mcp.json`) are preserved.
- **AI Project Root (Custom)** — Custom directory for AI/MCP config when **AI Project Root** is set to `Custom`. Relative paths are resolved against the `.toe` directory (e.g. `../` places config one level above the `.toe`); absolute paths are used as-is. Greyed out unless the menu is on `Custom`.

### Restoration

- **TOX Restore on Start** — Restore missing TOX-strategy COMPs from `.tox` files on project open (ON by default)
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
- **Content Safety** — What to do when TDN COMPs contain DATs or storage that would be lost on save: *Ask Each Save* (default) prompts before each save, *Always Externalize* auto-externalizes at-risk DATs without asking, *Never Ask* suppresses the check entirely. The *Never Ask* value is an opt-in escape hatch for power users — the save-time dialog no longer offers a single-click bypass button.
- **Export Project TDN** — Pulse to export the entire project network

### Logs

- **Verbose (Debug)** — Enable debug-level logging
- **Print to Textport** — Echo logs to the textport
- **Log to File** — Enabled by default, writes to `logs/<project_name>_YYMMDD.log`

## Settings Persistence

Embody automatically saves your parameter settings to a `config.json` file in the `.embody/` folder so they survive upgrades, crashes, and force-quits.

- **Location**: `.embody/` folder at the git root (next to `.mcp.json`), or the project folder if no git repo
- **When saved**: Automatically on every parameter change (debounced to 1 frame)
- **When restored**: On every project open (frame 5), and on fresh install after dropping in a new `.tox`
- **What's saved**: Folder path, Envoy config, tag names, tag colors, TDN settings, logging options, and other user-configurable parameters. Read-only status fields and runtime state are excluded

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
