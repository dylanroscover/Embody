# Getting Started

## Project Setup

Your TouchDesigner `.toe` file should live inside a **git repository**. Embody writes externalized files relative to the `.toe` location (`project.folder`), so your repo structure will typically look like:

```
my-project/              ← git repo root
├── .gitignore
├── my-project.toe       ← your TouchDesigner project
├── base1/               ← externalized COMPs and DATs
│   ├── base2.tox        ←   (folder structure mirrors your TD network)
│   └── text1.py
└── ...
```

### Auto-managed `.gitignore`

When Envoy starts, it automatically adds the following entries to your `.gitignore` if they're not already present:

| Entry | Purpose |
|-------|---------|
| `.venv/` | Python virtual environment (auto-created for Envoy dependencies) |
| `.mcp.json` | MCP client config (auto-generated per machine) |
| `.claude/` | Claude Code session data |
| `__pycache__/` | Python bytecode cache |
| `.DS_Store` | macOS Finder metadata |

!!! tip
    You may also want to gitignore your `logs/` directory if you don't need log history in version control.

## Installation

1. **Download** the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/main/release) folder
2. **Drag and drop** it into your TouchDesigner project

That's it. Embody is a self-contained component — no external dependencies needed for the core externalization features.

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
