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

2. **Work normally**: Save your project with ++ctrl+s++ — Embody automatically updates any dirty COMPs. DATs sync through TouchDesigner's native Sync to File mechanism.

3. **Save as you work**: Press ++ctrl+shift+u++ to update all dirty externalizations, or ++ctrl+alt+u++ to save just the COMP you're currently inside.

!!! tip
    To externalize an entire project at once, enable the **Externalize Full Project** option on the Embody COMP. Otherwise, externalize operators selectively with ++lctrl+lctrl++.

## Everyday Workflow

Once set up, Embody works in the background:

- **Auto-save on project save**: Saving your project (++ctrl+s++) autosaves all modified (dirty) COMPs. DATs synchronize automatically via their Sync to File parameter.
- **Quick save**: Use ++ctrl+shift+u++ to update only dirty COMPs, or ++ctrl+alt+u++ to save just the COMP you're currently inside (useful for large projects).
- **Parameter tracking**: Embody tracks all parameter values on externalized COMPs. When any parameter changes (not just network edits), that COMP is automatically marked dirty with a "Par" indicator.
- **Cross-platform**: All file paths are normalized to forward slashes (`/`), so teams on mixed Windows/macOS platforms can collaborate without path-related merge conflicts.
