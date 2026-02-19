# 🏷️ Embody

### ⚡ Automated Externalization and AI Integration for TouchDesigner

💾 **TouchDesigner 2025.32050** (Windows / macOS) &nbsp;|&nbsp; 📦 **v5.0.86**

🎬 [YouTube Demo/Tutorial](https://www.youtube.com/watch?v=lR3adD3Cw5s)

---

## 📖 Overview

TouchDesigner stores projects in binary `.toe` files that are impossible to diff or merge in git. **Embody** solves this by automatically externalizing your COMPs and DATs to version-control-friendly files (`.tox`, `.py`, `.json`, `.glsl`, etc.) in a folder structure that mirrors your network hierarchy. Tag any operator with a double-tap of left Ctrl, save your project, and Embody keeps everything in sync.

Embody also includes **Envoy**, an embedded [MCP](https://modelcontextprotocol.io/) server that lets AI coding assistants like [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Cursor](https://www.cursor.com/), and [Windsurf](https://windsurf.com/) create, modify, connect, and query operators in your live TouchDesigner session — all through natural language. And with the **TDN** network format, you can export your entire operator network to human-readable, diffable JSON.

| | Feature | What It Does |
|---|---------|-------------|
| 🔄 | **Automated Externalization** | Tags COMPs and DATs, keeps external files in sync with your `.toe` on every save |
| 🤖 | **Envoy MCP Server** | 40+ tools let AI assistants create operators, set parameters, wire connections, and more |
| 📄 | **TDN Network Format** | Export/import operator networks as diffable JSON for code review and snapshots |

---

## 📑 Table of Contents

- [🔄 Externalization](#-externalization)
  - [Project Setup](#-project-setup)
  - [Getting Started](#-getting-started)
  - [Workflow](#-workflow)
  - [Keyboard Shortcuts](#%EF%B8%8F-keyboard-shortcuts)
  - [Manager UI](#-manager-ui)
  - [Supported Operators & Formats](#-supported-operators--formats)
  - [Externalization Details](#-externalization-details)
- [🤖 Envoy MCP Server](#-envoy-mcp-server)
  - [Getting Started](#%EF%B8%8F-getting-started-1)
  - [What Can Envoy Do?](#%EF%B8%8F-what-can-envoy-do)
  - [Configuring the Port](#-configuring-the-port)
  - [CLAUDE.md Auto-Generation](#-claudemd-auto-generation)
- [📄 TDN Network Format](#-tdn-network-format)
  - [Exporting a Network](#-exporting-a-network)
  - [Importing a Network](#-importing-a-network)
  - [Per-COMP Export Mode](#-per-comp-export-mode)
- [📋 Logging](#-logging)
- [🧪 Test Framework](#-test-framework)
- [🔧 Troubleshooting](#-troubleshooting)
- [📜 Version History](#-version-history)
- [🤝 Contributors](#-contributors)

---

# 🔄 Externalization

## 📁 Project Setup

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

### 📝 Auto-managed `.gitignore`

When Envoy starts, it automatically adds the following entries to your `.gitignore` if they're not already present:

| Entry | Purpose |
|-------|---------|
| `.venv/` | Python virtual environment (auto-created for Envoy dependencies) |
| `.mcp.json` | MCP client config (auto-generated per machine) |
| `.claude/` | Claude Code session data |
| `__pycache__/` | Python bytecode cache |
| `*.lck` | TouchDesigner lock files |
| `.DS_Store` | macOS Finder metadata |

If no git repository is found, Envoy will offer to initialize one for you — or you can start without git (auto-configuration is skipped in that case).

> 💡 You may also want to gitignore `dev/logs/` if you don't need log history in version control.

## 🚀 Getting Started

1. **📥 Download**: Drag and drop the Embody `.tox` from the [`/release`](release/) folder into your TouchDesigner project.

2. **🏷️ Tag operators**: Select any COMP or DAT and press `lctrl` twice in a row. A tag appears indicating the operator is queued for externalization.

3. **▶️ Initialize**: Press `ctrl + shift + u` (or pulse the `Enable/Update` button on the Embody COMP). Embody externalizes all tagged operators to a folder structure mirroring your network.

4. **💾 Work normally**: Save your project with `ctrl + s` — Embody automatically updates any dirty COMPs. DATs sync through TouchDesigner's native Sync to File mechanism.

> 💡 If no operators are tagged, Embody will externalize all eligible COMPs and DATs, which may slow down complex projects. Tagging selectively is recommended.

## 🔄 Workflow

Embody keeps your external files updated as you work:

- **🔃 Auto-save on project save**: Saving your project (`ctrl + s`) autosaves all modified (dirty) COMPs. DATs synchronize automatically via their Sync to File parameter.
- **⚡ Quick save**: Use `ctrl + shift + u` to update only dirty COMPs, or `ctrl + alt + u` to save just the COMP you're currently inside (useful for large projects).
- **🔍 Parameter change detection**: Embody tracks all parameter values on externalized COMPs. When any parameter changes (not just network edits), that COMP is automatically marked dirty with a "Par" indicator — ensuring parameter tweaks are never lost.
- **🌐 Cross-platform compatibility**: All file paths are normalized to forward slashes (`/`), so teams on mixed Windows/macOS platforms can collaborate without path-related merge conflicts.

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `lctrl + lctrl` | 🏷️ Tag the selected operator for externalization (press left control twice) |
| `ctrl + shift + u` | 🔄 Initialize/update all externalizations |
| `ctrl + alt + u` | 💾 Save only the current COMP you're working inside |
| `ctrl + shift + o` | 📋 Open the Manager UI |
| `ctrl + shift + e` | 📄 Export entire project network to `.tdn` file |
| `ctrl + alt + e` | 📄 Export current COMP network to `.tdn` file |

## 📋 Manager UI

Press `ctrl + shift + o` to open the Manager window:

- **🌲 TreeLister View**: Hierarchical view of all externalized operators organized by path.
- **🔴 Status Indicators**: Shows dirty state for each operator (network changes or parameter changes marked as "Par").
- **🏗️ Build Information**: Displays build number, TouchDesigner build, and timestamp for each externalized COMP.
- **⚡ Quick Actions**:
  - 🖱️ Click to navigate to any operator
  - 📂 Open file location in your system file browser
  - 🔄 Refresh to update dirty states
  - 🔍 Filter/search through externalized operators

## 📦 Supported Operators & Formats

### COMPs
All COMPs except engine, time, and annotate — externalized as `.tox` files.

### DATs

| DAT Type |
|----------|
| Text DAT |
| Table DAT |
| Execute DAT |
| Parameter Execute DAT |
| Parameter Group Execute DAT |
| CHOP Execute DAT |
| DAT Execute DAT |
| OP Execute DAT |
| Panel Execute DAT |

### 📁 Supported File Formats

| Family | Formats |
|--------|---------|
| COMPs | `.tox` |
| DATs | `.py`, `.json`, `.xml`, `.html`, `.glsl`, `.frag`, `.vert`, `.txt`, `.md`, `.rtf`, `.csv`, `.tsv`, `.dat` |

## 🔧 Externalization Details

### ✨ Features
- 📊 Adds and updates `Build Number`, `Touch Build`, and `Build Date` parameters in an `About` page on every externalized COMP, for robust version tracking.
- ⚠️ Prompts whether to reference or clone an operator when a duplicate file path is detected (see [Duplicate Path Handling](#-duplicate-path-handling)).
- 🚫 Prevents clones and replicants (and their children) from being externalized.
- 🌍 Can externalize the entire project in one click with the `Externalize Full Project` pulse.
- 📊 Isolated data/logic pattern with an `externalizations` tableDAT outside of Embody for easy updating and management.
- 🕐 UTC timestamps for synchronized international workflows.
- 🛡️ Safe file deletion — only removes files Embody created, never deletes untracked files.
- 🔍 Automatic parameter change detection marks COMPs dirty when any parameter is modified.

### 📂 Folder Configuration

The externalization folder can be configured in several ways:

- **📁 Static Path**: Set a folder name like `externals` to save to `{project.folder}/externals/`
- **🐍 Expression Mode**: Use Python expressions for dynamic paths (e.g., `project.folder + '/build_' + str(app.build)`)
- **📁 Existing Folders**: You can point Embody at a folder containing other files — Embody will only manage its own tracked files and leave others untouched.

> 📝 When changing the folder location, Embody will migrate tracked files to the new location and clean up empty directories in the old location.

### 🔀 Duplicate Path Handling

When Embody detects two operators pointing to the same external file, it prompts you with options:

- **🔗 Reference**: Both operators share the same external file. The new operator receives a `clone` tag and changes to either will affect the shared file.
- **📋 Duplicate**: Create a new, separate externalization for the operator with its own file path.
- **❌ Cancel**: Take no action.

Enable or disable this check with the `Detect Duplicate Paths` parameter.

### 🔄 Resetting

To completely reset and remove externalizations, pulse the `Disable` button.

> 🛡️ This will delete only the files that Embody created (tracked in the externalizations table). Any other files in the externalization folder will be preserved. Empty folders may be removed, but folders containing untracked files will not be touched.

Options when disabling:
- **✅ Yes, keep Tags**: Remove externalizations but keep the tags on operators for easy re-enabling.
- **🗑️ Yes, remove Tags**: Remove externalizations and all Embody tags from operators.

### 📊 Externalizations Table

Embody maintains an `externalizations` tableDAT outside the Embody component with the following columns:

| Column | Description |
|--------|-------------|
| `path` | TouchDesigner operator path (e.g., `/project/base1`) |
| `type` | Operator type (e.g., `base`, `text`, `table`) |
| `rel_file_path` | Relative file path from project folder |
| `timestamp` | Last save time in UTC |
| `dirty` | Dirty state (`True`, `False`, or `Par` for parameter changes) |
| `build` | Build number (COMPs only) |
| `touch_build` | TouchDesigner build version (COMPs only) |

This table serves as the source of truth for what files Embody manages. Only files listed here will ever be deleted by Embody.

---

# 🤖 Envoy MCP Server

Embody includes **Envoy**, an embedded [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) server that lets AI coding assistants interact with TouchDesigner programmatically. With Envoy running, an MCP-compatible client can create operators, set parameters, wire connections, export networks, manage externalizations, and more — all through natural language conversation.

Envoy works with any MCP client, including [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Cursor](https://www.cursor.com/), [Windsurf](https://windsurf.com/), and others that support the MCP protocol.

## ⚙️ Getting Started

You'll need an MCP-compatible client such as [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Cursor](https://www.cursor.com/), or [Windsurf](https://windsurf.com/). Embody automatically installs all server-side dependencies (`mcp`, `uvicorn`, etc.) when Envoy is first enabled — no manual Python setup required.

1. **🔛 Enable Envoy**: Toggle the `Envoyenable` parameter on the Embody COMP
2. **🚀 Server starts**: Envoy runs on `localhost:9876` (configurable via `Envoyport`)
3. **📄 Auto-configuration**: Envoy creates a `.mcp.json` file in your git repo root automatically
4. **🔌 Connect your MCP client**: Start a new Claude Code session (or restart your IDE) — it picks up the `.mcp.json` automatically

> 💡 Auto-configuration requires your `.toe` project to be inside a **git repository** (see [Project Setup](#-project-setup)). If your project isn't in a git repo, create `.mcp.json` manually in your project directory:
>
> ```json
> {
>   "mcpServers": {
>     "envoy": {
>       "type": "http",
>       "url": "http://localhost:9876/mcp"
>     }
>   }
> }
> ```

## 🛠️ What Can Envoy Do?

Envoy exposes 40+ MCP tools organized into categories:

| Category | Examples |
|----------|---------|
| 🧱 **Operator Management** | Create, delete, copy, rename, query operators |
| 🎛️ **Parameters** | Get/set values, expressions, bind expressions |
| 🔗 **Connections** | Wire operators together, disconnect inputs |
| 📝 **DAT Content** | Read/write text and table data |
| 🧩 **Extensions** | Create TD extensions with proper boilerplate |
| 📌 **Annotations** | Create network boxes, comments, annotate groups |
| 🔍 **Diagnostics** | Check errors, get performance data, introspect API |
| 🏷️ **Embody Integration** | Tag, save, query externalizations |
| 📄 **TDN Export/Import** | Export/import network snapshots as JSON |
| 🐍 **Code Execution** | Run arbitrary Python in TouchDesigner |

For the complete tool reference, see the [CLAUDE.md](CLAUDE.md) file.

## 🔧 Configuring the Port

Change the `Envoyport` parameter on the Embody COMP. If the server is running, it automatically restarts on the new port and updates `.mcp.json`.

## 📄 CLAUDE.md Auto-Generation

When Envoy starts, it generates a `CLAUDE.md` file in your project root. This file provides Claude Code with context about TouchDesigner development patterns, the MCP tool reference, testing conventions, and project-specific guidance.

---

# 📄 TDN Network Format

TDN (TouchDesigner Network) is a JSON-based file format for exporting TouchDesigner operator networks as human-readable, diffable text. Unlike binary `.toe` and `.tox` files, `.tdn` files can be meaningfully diffed in git, making it easy to review changes to your network structure.

## 📤 Exporting a Network

- **🌐 Entire project**: Press `ctrl + shift + e` to export all operators to a `.tdn` file
- **📦 Current COMP only**: Press `ctrl + alt + e` to export just the COMP you're working inside
- **🤖 Via Envoy**: Use the `export_network` MCP tool for programmatic export

TDN files store only non-default parameter values, keeping the output minimal. DAT content (scripts, tables) can optionally be included.

## 📥 Importing a Network

Use the `import_network` Envoy MCP tool to recreate a network from a `.tdn` file. The import process handles operator creation, custom parameters, parameter values, flags, wiring, DAT content, and positioning in the correct order.

## 📂 Per-COMP Export Mode

For large projects, TDN supports splitting the export into one file per COMP, creating a directory structure that mirrors your TouchDesigner network hierarchy.

## 📘 Full Specification

See [docs/TDN.md](docs/TDN.md) for the complete format specification including all field definitions, value serialization rules, and import process details.

---

## 📋 Logging

Embody provides a multi-destination logging system:

- **📁 File logging** (default): Logs are written to `dev/logs/<project_name>_YYMMDD.log`. Files auto-rotate at 10 MB.
- **📺 FIFO DAT**: Recent log entries are visible in TouchDesigner's network editor.
- **🖨️ Textport**: Enable the `Print` parameter to echo logs to the textport.
- **💾 Ring buffer**: The most recent 200 entries are accessible via the Envoy `get_logs` MCP tool.

Log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `SUCCESS`.

Use from anywhere in your project:
```python
op.Embody.Log('Something happened', 'INFO')
op.Embody.Warn('Check this out')
op.Embody.Error('Something broke')
```

---

## 🧪 Test Framework

Embody includes a comprehensive automated test suite with **27 test suites** covering core externalization, MCP tools, TDN format, and server lifecycle. Tests run inside TouchDesigner using a custom test runner with sandbox isolation.

### ▶️ Running Tests

From the TouchDesigner textport:
```python
# Run all tests (non-blocking, one test per frame)
op.unit_tests.RunTests()

# Run a specific suite
op.unit_tests.RunTests(suite_name='test_path_utils')
```

Via Envoy MCP: use the `run_tests` tool.

### 📊 Test Coverage

- **13 core suites**: Externalization lifecycle, file management, tagging, rename/move, delete cleanup, path utilities, parameter tracking, logging
- **11 MCP tool suites**: Operators, parameters, DAT content, connections, annotations, extensions, diagnostics, flags/position, code execution, externalization, performance
- **2 TDN suites**: Export/import, helper functions
- **1 infrastructure suite**: Server lifecycle

---

## 🔧 Troubleshooting

### 🐛 Debug Mode
For verbose path logging and troubleshooting, enable debug mode by setting `debug_mode = True` in the EmbodyExt extension. This will log detailed path information to the textport.

### ⚠️ Common Issues
- **⏸️ Timeline Paused**: Embody requires the timeline to be running. A warning will appear in the textport if the timeline is paused.
- **🔗 Clone/Replicant Operators**: These cannot be externalized. Embody will show a warning if you try to tag them.
- **🚫 Engine COMPs**: Engine, time, and annotate COMPs are not supported for externalization.

---

## 📜 Version History
- **5.0.86**: Current release — Manager UI refactored into modular externalized components
- **5.0.71**: Rename Claudius to Envoy, expand README and help text
- **5.0.61**: Rename MCP tools for consistency, add auto-restart on port change, expand testing documentation
- **5.0.59**: Migrate tests to externalized DATs, add deferred test runner (one test per frame)
- **5.0.56**: Rewrite test runner, fix `run()` safety, add 6 new test suites, update documentation
- **5.0**: Major release — add Envoy MCP server (40+ tools for Claude Code integration), TDN network format (JSON export/import), comprehensive test framework (26 suites), structured logging system, CLAUDE.md auto-generation, cross-platform macOS support
- **4.7.14**: Safe file deletion - Embody now only deletes files it created. Untracked files in the externalization folder are preserved during disable/migration operations.
- **4.7.11**: Cross-platform path handling (forward slashes on all platforms) + code cleanup
- **4.7.6**: Build save increment bug fix
- **4.7.5**:
    - ui.rolloverOp refactor
    - Restore handling of dnd COMP auto-populated externaltox pars
    - Cache parameters correctly between tox saves
    - Add parameter updated coloring for dirty buttons in UI
    - Path lib implementation improvements / added consistency
    - Auto refresh on UI maximize
    - Do not auto update when adding an externalization
    - Ignore untagged COMPs when checking for duplicate paths
- **4.6.4**:
    - Add About page to externalized COMPs with:
        - Build Number
        - Touch Build
        - Build Date (time tox was saved)
    - Add Build/Touch Build to externalization table + Lister
    - Window resizing support and cleaned up min/max button methods
- **4.5.23**:
    - Fix deletion of old file storage after renaming operation
    - Cleanup network
    - Tagging optimization
    - Cleanup folder structure
    - Remove folderDAT
    - Fix duplicated rows from externalizations tsv git merge conflicts
- **4.5.19**: Allow master clones with clone pars to be externalized, Setup menu cleanup
- **4.5.17**: Bug fixes, smaller minimized window footprint
- **4.5.2**:
    - Add tsv support
    - Add Clone tag for shared external paths
    - Handle drag and dropped COMP auto-populated externaltox pars
    - Detect dirty COMP par changes
- **4.4.128**: Add support for COMPs with empty/error prone clone expressions (such as rollovers in Probe)
- **4.4.127**: Added textport warning for when timeline is paused
- **4.4.126**: Clean up Save and dirtyHandler methods, auto set enableexternaltox par to ensure saves
- **4.4.125**: Bug fix for handling empty externalTimeStamp value
- **4.4.124**: More bug fixes with file handling
- **4.4.119**: mouseinCHOP chopexecDAT optimization
- **4.4.117**: Additional externalization folder removal bug fixes
- **4.4.116**: UI color and icon refinement
- **4.4.113**: externalization folder bug fixes
- **4.4.112**: engine/annotateCOMP Tagger handling
- **4.4.111**: Bug fix for Disable method
- **4.4.109**: Correctly deletes previous externalization folder when changed
- **4.4.107**: Multi-display support for Tagger, minor Windows fixes
- **4.4.104**: Added TreeLister, improved Tagger stability, color theme updates
- **4.4.74**:
    - Added feature for externalizating full project automatically
    - Support for handling deletion and re-creation (redo) of COMPs/DATs
    - Support for renaming COMPs and DATs
    - Support for moving COMPs/DATs
    - Various small bug fixes and feature improvements
- **4.3.134**: Adding missing reference to list COMP
- **4.3.133**: Fixed externalizations folder button on macOS, fixed filter display, added clear button to filter UI
- **4.3.128**: Fixed abs path bug, added support for macOS Finder and keyboard shortcuts
- **4.3.122**: Separated logic/data for easier Embody updates, bug fix for checking for duplicate OPs
- **4.3.48**: Handling for duplicate OP tox/file paths.
- **4.3.43**: Switched to UTC, added Save/Table DAT buttons, refactored tagging, better externaltox handling.
- **4.2.101**: Fixed keyboard shortcut bug, updated to TouchDesigner 2023.
- **4.2.98**: Added handling for Cloners/Replicants.
- **4.2.0**: UI fixes, path cleanup, folder switching fixes.
- **4.1.0**: Improved file/folder management, bug fixes.
- **4.0.0**: Added support for various file formats, parameter improvements.
- **3.0.5**: Tweaked reset function.
- **3.0.4**: Updated versioning system.
- **3.0.3**: Updated to TouchDesigner 2022.
- **3.0.2**: Added Manager UI, clarified commands, added deletion mechanisms.
- **3.0.1**: Added keyboard shortcuts, minor bug fixes.
- **3.0.0**: Initial release.

---

## 🤝 Contributors

Originally developed by [Tim Franklin](https://github.com/franklin113/). Refactored entirely by Dylan Roscover, with inspiration and guidance from Elburz Sorkhabi, Matthew Ragan and Wieland Hilker.

## 📄 License

[TEC Friendly License v1.0](LICENSE)
