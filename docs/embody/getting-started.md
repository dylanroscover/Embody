# Getting Started

## Project Setup

Embody writes externalized files relative to your `.toe` location (`project.folder`). Your project folder will typically look like:

```
my-project/              ← project folder (optionally a git repo)
├── my-project.toe       ← your TouchDesigner project
├── base1/               ← externalized COMPs and DATs
│   ├── base2.tox        ←   (folder structure mirrors your TD network)
│   └── text1.py
└── ...
```

**Git is not required.** Embody's externalization, auto-restoration, and Envoy MCP features all work without version control. If you do use git, you get diffable history and collaboration — but it's entirely optional.

### Auto-managed `.gitignore`

If your project is inside a git repository, Envoy automatically adds the following entries to your `.gitignore` when it starts:

| Entry | Purpose |
|-------|---------|
| `Backup/` | TouchDesigner versioned `.toe` backups |
| `logs/` | Embody log files |
| `CrashAutoSave*` | TouchDesigner crash auto-save files |
| `.venv/` | Python virtual environment (auto-created for Envoy dependencies) |
| `.mcp.json` | MCP client config (auto-generated per machine) |
| `.embody/*` | Envoy runtime files (instance registry, bridge, cache) |
| `!.embody/project.json` | Un-ignores the committed `td_build` pin so it stays tracked |
| `.claude/settings.local.json` | Claude Code per-machine permissions |
| `.claude/projects/` | Claude Code session data |
| `__pycache__/` | Python bytecode cache |
| `.DS_Store` | macOS Finder metadata |

## Installation

1. **Download** the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/main/release) folder
2. **Drag and drop** it into your TouchDesigner project

Embody initializes automatically over the next several frames:

- **Frame 15**: Creates (or reconnects to) the `externalizations` tableDAT in the same container as Embody. If you're upgrading and a table already exists as a sibling, Embody reconnects to it without creating a duplicate.
- **Frame 30**: Runs `Verify()`, which checks whether this is a fresh install or an upgrade:
    - **Fresh install** (empty table): Embody runs quietly with no dialogs.
    - **Upgrade** (table has prior data): Embody quietly validates tracked operators — no dialog. The schema is migrated, paths are normalized, and every tracked row is checked for continuity; only genuinely changed operators are re-exported.

After verification, if Envoy is not yet enabled, the [Setup Wizard](setup-wizard.md) opens — a few quick screens covering Embody's mode, the AI assistant, and where config files go. Finishing it with an assistant selected will:

- Install Python MCP dependencies (~30 MB via `uv`)
- Start a local MCP server on the configured port
- Generate AI config files in your project root: `.mcp.json`, an always-written `AGENTS.md` (the universal standard read by all major AI tools), and client-specific config for the AI Client you select. For Claude Code that is `CLAUDE.md` plus a `.claude/` directory with [coding rules, skills, and slash commands](../envoy/claude-code.md); other clients get their own file (`.cursor/rules`, `.github/copilot-instructions.md`, `.windsurf/rules`, `GEMINI.md`, etc.)

You can close the wizard with **Not now** (nothing is changed) and re-open it anytime via the **Setup Wizard** parameter on the Embody page — or enable Envoy directly from the **Envoy** parameter page.

Embody is a self-contained component — no external dependencies are needed for the core externalization features.

## First Externalization

1. **Externalize operators**: Hover any COMP or DAT and press ++lctrl++ twice in a row. Embody opens the tagger UI for the operator under your cursor. For an untagged operator it lets you pick how to externalize it — a strategy (TOX or TDN) for a COMP, or a file format for a DAT; for an already-tagged operator it lets you switch strategy, remove the tag, or save the externalization.

2. **Update as you work**: Press ++ctrl+shift+u++ to update all dirty externalizations, or ++ctrl+alt+u++ to update just the COMP you're currently inside.

3. **Work with confidence**: Your externalizations are written to disk for diffs and AI context. On project open, TOX-strategy COMPs are always restored from `.tox` files and DATs sync from their externalized source files (`.py`, `.txt`, `.json`, ...). TDN-strategy COMPs are reconstructed from `.tdn` files **only in Roundtrip mode** — in the default Export-on-Save mode the `.toe` remains authoritative and is not rebuilt from `.tdn` on open. See [TDN Mode](externalization.md#tdn-mode-master-switch) for the tradeoffs.

!!! tip
    To externalize an entire project at once, enable the **Externalize Full Project** option on the Embody COMP. Otherwise, externalize operators selectively with ++lctrl+lctrl++.

## Everyday Workflow

Once set up, Embody works in the background:

- **Update externalizations**: Use ++ctrl+shift+u++ to update all dirty COMPs and DATs, or ++ctrl+alt+u++ to update just the COMP you're currently inside.
- **Automatic restoration**: On project open, Embody restores externalized operators from the files on disk. TOX-strategy COMPs are always restored from `.tox` files, and DATs always sync via TouchDesigner's native file parameter. TDN-strategy COMPs are reconstructed from `.tdn` files **only in Roundtrip mode** — the recommended Export-on-Save mode keeps the `.toe` as the source of truth and skips reconstruction on open. See [TDN Mode](externalization.md#tdn-mode-master-switch) for the tradeoffs.
- **Parameter tracking**: Embody tracks all parameter values on externalized COMPs. When any parameter changes (not just network edits), that COMP is automatically marked dirty with a "Par" indicator.
- **Cross-platform**: All file paths are normalized to forward slashes (`/`), so teams on mixed Windows/macOS platforms can collaborate without path-related merge conflicts.

## Removing Embody

There are two different "off switches", and they do different things:

- **Disable** (Embody page) removes Embody's externalization **tags** from your operators and stops tracking them. Your externalized files stay on disk. This is reversible — pulse **Enable / Update** to start tracking again. Use it to pause Embody, not to remove it.
- **Uninstall** (Embody page, right below Disable) reverses Embody's **install footprint** — the files and settings Embody added to your project when you set it up. Use it when you want Embody gone from a repo.

### The Uninstall button

Pulsing **Uninstall** first shows a confirmation dialog that spells out exactly what will happen before anything is touched:

- **Removed** — Embody-generated AI-assistant config (`CLAUDE.md` / `AGENTS.md` / `.claude/` / `.cursor/` / …), the Embody `.venv`, and the `.embody/` state folder.
- **Modified** — shared files where Embody only *strips its own block or key*, leaving your content intact: `.gitignore`, `.gitattributes`, and the `envoy` server entry in `.mcp.json` (your other MCP servers are kept).
- **Un-set** — the git config keys for the `.tdn` diff driver.
- **Kept** — anything Embody can't prove it owns: a generated file you edited, `settings.local.json`, an unrecorded-looking venv. These are flagged and left untouched.

It only proceeds when you confirm. Cancelling — or triggering it during a save or a test run — does nothing.

**What Uninstall never touches:** your externalized `.tox` / `.tdn` / `.py` files, and the Embody COMP itself. To finish removing Embody from a `.toe`, delete the Embody COMP after uninstalling (run **Disable** first if you also want the externalized files reabsorbed into the `.toe`).

### Previewing without removing anything

To see the full reversal plan without changing anything, call `PreviewUninstall()` from a Textport or via Envoy — it logs the same **remove / modify / un-set / keep** breakdown the dialog shows, but deletes nothing:

```python
op.Embody.PreviewUninstall()
```

The scriptable, non-interactive form is `op.Embody.Uninstall(confirm=True)` (it refuses without `confirm=True`); pass `include_review=True` to also remove the flagged "kept" items.
