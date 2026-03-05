# 🪞 Embody

### Supercharge Your TouchDesigner Workflow With AI

**TouchDesigner 2025.32280** (Windows / macOS) &nbsp;|&nbsp; **v5.0.190**

[YouTube Demo/Tutorial](https://www.youtube.com/watch?v=lR3adD3Cw5s) &nbsp;|&nbsp; [Full Documentation](docs/)

---

## Overview

Build faster. Debug smarter. Let AI handle the tedious parts while you focus on what matters — your creative vision.

**Envoy**, Embody's embedded [MCP](https://modelcontextprotocol.io/) server, connects AI assistants like [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Cursor](https://www.cursor.com/), and [Windsurf](https://windsurf.com/) to your live TouchDesigner session. Create operators, wire connections, set parameters, write extensions, and debug errors — all through natural conversation. No copy-pasting code. No describing your network in chat. You stay in control while AI does the heavy lifting.

**Embody** makes your TouchDesigner projects version-control-friendly. Tag any operator with a double-tap of left Ctrl, and Embody automatically externalizes your COMPs and DATs to diffable files (`.tox`, `.py`, `.json`, `.glsl`, etc.) in a folder structure that mirrors your network hierarchy. On project open, everything is restored from the files on disk — your externalized files are the source of truth.

**TDN** (TouchDesigner Network) takes it further — export your entire operator network to human-readable JSON. Review structural changes in pull requests, snapshot configurations, and reconstruct entire networks from text.

![Embody Manager UI](docs/assets/embody-screenshot.png)

| | Feature | What It Does |
|---|---------|-------------|
| 📦 | **Automated Externalization** | Tags COMPs and DATs, keeps external files in sync — auto-restores everything from disk on project open |
| 🤖 | **Envoy MCP Server** | 40+ tools let AI assistants create operators, set parameters, wire connections, and more |
| 📄 | **TDN Network Format** | Export/import operator networks as diffable JSON for code review and snapshots |
| 📤 | **Portable Tox Export** | Export any COMP as a self-contained `.tox` with all external references stripped |

---

## 🚀 Getting Started

### 1. Project Setup

Your TouchDesigner `.toe` file should live inside a **git repository**. Embody writes externalized files relative to the `.toe` location, so your repo structure will look like:

```
my-project/              <- git repo root
├── .gitignore
├── my-project.toe       <- your TouchDesigner project
├── base1/               <- externalized COMPs and DATs
│   ├── base2.tox
│   └── text1.py
└── ...
```

### 2. Install and Tag

1. **Download**: Drag and drop the Embody `.tox` from the [`/release`](release/) folder into your TouchDesigner project.
2. **Tag operators**: Select any COMP or DAT and press `lctrl` twice in a row.
3. **Initialize**: Press `ctrl + shift + u` to externalize all tagged operators.
4. **Work normally**: Press `ctrl + shift + u` as you work to save dirty externalizations. On project open, Embody restores everything from the files on disk automatically.

> If no operators are tagged, Embody will externalize all eligible COMPs and DATs, which may slow down complex projects. Tagging selectively is recommended.

### 3. ⌨️ Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `lctrl + lctrl` | Tag or manage the operator under the cursor |
| `ctrl + shift + u` | Initialize/update all externalizations |
| `ctrl + alt + u` | Save only the current COMP |
| `ctrl + shift + o` | Open the Manager UI |
| `ctrl + shift + e` | Export entire project to `.tdn` file |
| `ctrl + alt + e` | Export current COMP to `.tdn` file |

For detailed feature documentation — supported formats, folder configuration, duplicate handling, Manager UI, and more — see the [Embody docs](docs/embody/).

---

## 🤖 Envoy MCP Server

Embody includes **Envoy**, an embedded [MCP](https://modelcontextprotocol.io/) server that lets AI coding assistants interact with TouchDesigner programmatically.

### Quick Start

1. **Enable Envoy**: Toggle the `Envoyenable` parameter on the Embody COMP
2. **Server starts**: Envoy runs on `localhost:9870` (configurable via `Envoyport`)
3. **Auto-configuration**: Envoy creates a `.mcp.json` file in your git repo root
4. **Connect your MCP client**: Start a new Claude Code session (or restart your IDE) — it picks up `.mcp.json` automatically

> If your project isn't in a git repo, see the [manual setup instructions](docs/envoy/setup.md).

### Capabilities

Envoy exposes 40+ MCP tools for operator management, parameters, connections, DAT content, extensions, annotations, diagnostics, Embody integration, TDN export/import, and code execution. See the [full tools reference](docs/envoy/tools-reference.md).

When Envoy starts, it generates a `CLAUDE.md` file in your project root with context about TD development patterns, the MCP tool reference, and project-specific guidance.

---

## 📄 TDN Network Format

TDN (TouchDesigner Network) is a JSON-based format for exporting operator networks as human-readable, diffable text. Unlike binary `.toe` and `.tox` files, `.tdn` files can be meaningfully diffed in git.

- **Entire project**: `ctrl + shift + e`
- **Current COMP**: `ctrl + alt + e`
- **Via Envoy**: `export_network` MCP tool
- **Import**: `import_network` MCP tool

See the [full TDN specification](docs/tdn/specification.md) for format details, import process, and round-trip guarantees.

---

## 📋 Logging

Embody provides a multi-destination logging system:

- **File logging** (default): `dev/logs/<project_name>_YYMMDD.log`, auto-rotates at 10 MB
- **FIFO DAT**: Recent entries visible in the TD network editor
- **Textport**: Enable the `Print` parameter to echo logs
- **Ring buffer**: Last 200 entries via the Envoy `get_logs` MCP tool

```python
op.Embody.Log('Something happened', 'INFO')
op.Embody.Warn('Check this out')
op.Embody.Error('Something broke')
```

---

## 🧪 Testing

Embody includes **30 test suites** covering core externalization, MCP tools, TDN format, and server lifecycle. Tests run inside TouchDesigner using a custom test runner with sandbox isolation.

```python
op.unit_tests.RunTests()                              # All tests (non-blocking)
op.unit_tests.RunTests(suite_name='test_path_utils')   # Single suite
op.unit_tests.RunTestsSync()                           # All in one frame (blocks TD)
```

Via Envoy MCP: use the `run_tests` tool. See the [full testing docs](docs/testing.md) for coverage details and how to write new tests.

---

## ❓ Troubleshooting

- **Timeline Paused**: Embody requires the timeline to be running. A warning appears if paused.
- **Clone/Replicant Operators**: Cannot be externalized. Embody warns if you try to tag them.
- **Engine COMPs**: Engine, time, and annotate COMPs are not supported for externalization.

For more, see [Troubleshooting](docs/embody/troubleshooting.md).

---

## 📝 Version History

See the [full changelog](docs/changelog.md) for detailed version history.

**Recent releases:**

- **5.0.190**: Automatic restoration — TOX and TDN strategy COMPs are fully restored from disk on project open, documentation overhaul
- **5.0.178**: Reload from disk, full project TDN safety, continuity hardening
- **5.0.171**: Export Portable Tox, improved tag management, TDN error handling, window management refactor
- **5.0.140**: TDN strip/restore hardening, `file`/`syncfile` export, post-import validation, TDN restore UI, companion DAT reuse, bug fixes
- **5.0.130**: TDN strategy externalization, strip/restore save cycle, compact TDN format, per-COMP split export
- **5.0**: Major release — Envoy MCP server (40+ tools), TDN format, test framework (30 suites), structured logging, CLAUDE.md auto-generation, macOS support

---

## Contributors

Originally derived from [External Tox Saver](https://github.com/franklin113/External-Tox-Saver) by [Tim Franklin](https://github.com/franklin113/). Refactored entirely by Dylan Roscover, with inspiration and guidance from Elburz Sorkhabi, Matthew Ragan and Wieland Hilker.

## License

[TEC Friendly License v1.0](LICENSE)
