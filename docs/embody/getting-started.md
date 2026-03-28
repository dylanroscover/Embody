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
| `.venv/` | Python virtual environment (auto-created for Envoy dependencies) |
| `.mcp.json` | MCP client config (auto-generated per machine) |
| `.claude/settings.local.json` | Claude Code per-machine permissions |
| `.claude/projects/` | Claude Code session data |
| `.claude/envoy-bridge.py` | MCP transport bridge (auto-generated) |
| `__pycache__/` | Python bytecode cache |
| `.DS_Store` | macOS Finder metadata |

!!! tip
    You may also want to gitignore your `logs/` directory if you don't need log history in version control.

## Installation

1. **Download** the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/main/release) folder
2. **Drag and drop** it into your TouchDesigner project

Embody initializes automatically over the next two frames:

- **Frame 15**: Creates (or reconnects to) the `externalizations` tableDAT in the same container as Embody. If you're upgrading and a table already exists as a sibling, Embody reconnects to it without creating a duplicate.
- **Frame 30**: Runs `Verify()`, which checks whether this is a fresh install or an upgrade:
    - **Fresh install** (empty table): Embody runs quietly with no dialogs.
    - **Upgrade** (table has prior data): Embody prompts you to re-scan and validate tracked operators.

After verification, if Envoy is not yet enabled, Embody prompts you to set it up. Accepting will:

- Install Python MCP dependencies (~30 MB via `uv`)
- Start a local MCP server on the configured port
- Generate AI config files in your project root: `CLAUDE.md`, `.mcp.json`, and a `.claude/` directory with [coding rules, skills, and slash commands](../envoy/claude-code.md)

You can skip this and enable Envoy later from the **Envoy** tab.

Embody is a self-contained component — no external dependencies are needed for the core externalization features.

## First Externalization

1. **Externalize operators**: Select any COMP or DAT and press ++lctrl++ twice in a row. Embody tags the operator and externalizes it to disk in one step.

2. **Save as you work**: Press ++ctrl+shift+u++ to update all dirty externalizations, or ++ctrl+alt+u++ to save just the COMP you're currently inside.

3. **Work with confidence**: Your externalized files on disk are the source of truth. On project open, Embody automatically restores everything from disk — you never need to worry about losing externalized work.

!!! tip
    To externalize an entire project at once, enable the **Externalize Full Project** option on the Embody COMP. Otherwise, externalize operators selectively with ++lctrl+lctrl++.

## Everyday Workflow

Once set up, Embody works in the background:

- **Save externalizations**: Use ++ctrl+shift+u++ to save all dirty COMPs and DATs, or ++ctrl+alt+u++ to save just the COMP you're currently inside.
- **Automatic restoration**: On project open, Embody automatically restores all externalized operators from the files on disk. TOX-strategy COMPs are restored from `.tox` files, TDN-strategy COMPs are reconstructed from `.tdn` JSON files, and DATs sync via TouchDesigner's native file parameter. You do not need to save your `.toe` file to preserve externalized work.
- **Parameter tracking**: Embody tracks all parameter values on externalized COMPs. When any parameter changes (not just network edits), that COMP is automatically marked dirty with a "Par" indicator.
- **Cross-platform**: All file paths are normalized to forward slashes (`/`), so teams on mixed Windows/macOS platforms can collaborate without path-related merge conflicts.
