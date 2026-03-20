# 💬 Embody

**Have a conversation with TouchDesigner.**

![Version](https://img.shields.io/badge/version-5.0.235-blue)
![TouchDesigner](https://img.shields.io/badge/TouchDesigner-2025-orange)
![MCP Tools](https://img.shields.io/badge/MCP_tools-45-purple)
![License](https://img.shields.io/badge/license-TEC_Friendly-green)

[Full Documentation](https://dylanroscover.github.io/Embody/) &nbsp;|&nbsp; [Changelog](https://dylanroscover.github.io/Embody/changelog/)

---

TouchDesigner projects are binary `.toe` files — impossible to diff, merge, or review in git. Embody makes your TD projects readable: by AI, by git, and by you.

## What It Does

**Envoy**, Embody's embedded [MCP](https://modelcontextprotocol.io/) server, lets AI assistants like [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Cursor](https://www.cursor.com/), and [Windsurf](https://windsurf.com/) talk directly to your live TouchDesigner session. Create operators, wire connections, set parameters, write extensions, and debug errors — all through natural conversation. No copy-pasting code. No describing your network in chat.

**Embody** externalizes your operators to diffable files (`.tox`, `.py`, `.json`, `.glsl`, etc.) in a folder structure that mirrors your network hierarchy. Tag operators, save with `ctrl + shift + u`, and everything restores from disk automatically on project open — your externalized files are the source of truth.

**TDN** (TouchDesigner Network) exports your entire operator network to human-readable JSON — a structured language that both humans and LLMs can read, diff, and reconstruct. Review structural changes in pull requests, snapshot configurations, or hand an LLM a complete picture of your network.

![Embody Manager UI](docs/assets/embody-screenshot.png)

| | Feature | What It Does |
|---|---------|-------------|
| 📦 | **Automated Externalization** | Tags COMPs and DATs, keeps external files in sync — auto-restores everything from disk on project open |
| 🤖 | **Envoy MCP Server** | 45 tools let AI assistants create operators, set parameters, wire connections, and more |
| 📄 | **TDN Network Format** | Export/import operator networks as diffable JSON for code review and snapshots |
| 📤 | **Portable Tox Export** | Export any COMP as a self-contained `.tox` with all external references stripped |

---

## Quick Start

### 1. Project Setup

Your TouchDesigner `.toe` file should live inside a **git repository**. Embody writes externalized files relative to the `.toe` location:

```
my-project/              ← git repo root
├── .gitignore
├── my-project.toe       ← your TouchDesigner project
├── base1/               ← externalized operators
│   ├── base2.tox        ← COMP (TOX strategy)
│   ├── base3.tdn        ← COMP (TDN strategy — diffable JSON)
│   └── text1.py         ← DAT
└── ...
```

### 2. Install and Tag

1. **Download** the Embody `.tox` from [`/release`](release/) and drag it into your TouchDesigner project
2. **Tag operators** — select any COMP or DAT and press `lctrl` twice to tag and externalize it
3. **Work normally** — press `ctrl + shift + u` to save all changes, or `ctrl + alt + u` to save only the current COMP. On project open, Embody restores everything from disk automatically

> **Tip:** If no operators are tagged, Embody will externalize all eligible COMPs and DATs, which may slow down complex projects. Tagging selectively is recommended.

### 3. Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `lctrl + lctrl` | Tag or manage the operator under the cursor |
| `ctrl + shift + u` | Initialize / update all externalizations |
| `ctrl + alt + u` | Save only the current COMP |
| `ctrl + shift + o` | Open the Manager UI |
| `ctrl + shift + e` | Export entire project to `.tdn` file |
| `ctrl + alt + e` | Export current COMP to `.tdn` file |

For supported formats, folder configuration, duplicate handling, Manager UI, and more — see the [Embody docs](https://dylanroscover.github.io/Embody/embody/).

---

## Envoy MCP Server

Embody includes **Envoy**, an embedded [MCP](https://modelcontextprotocol.io/) server that gives AI coding assistants direct access to your live TouchDesigner session.

### Setup

1. **Enable Envoy** — toggle the `Envoyenable` parameter on the Embody COMP
2. **Server starts** on `localhost:9870` (configurable via `Envoyport`)
3. **Auto-configuration** — Envoy creates a `.mcp.json` in your git repo root
4. **Connect** — open a Claude Code session (or restart your IDE) in the repo root — it picks up `.mcp.json` automatically

If your project isn't in a git repo, add `.mcp.json` manually to your project root:

```json
{
  "mcpServers": {
    "envoy": {
      "type": "http",
      "url": "http://localhost:9870/mcp"
    }
  }
}
```

### Tools at a Glance

| Tool | What It Does |
|------|-------------|
| `create_op` | Create any operator type in any network |
| `set_parameter` | Set values, expressions, or bind modes on any parameter |
| `connect_ops` | Wire operators together |
| `execute_python` | Run arbitrary Python in TD's main thread |
| `export_network` | Export networks to diffable `.tdn` JSON |
| `create_extension` | Scaffold a full extension (COMP + DAT + wiring) |
| `get_op_errors` | Inspect errors on any operator and its children |

...and 37 more. See the [full tools reference](https://dylanroscover.github.io/Embody/envoy/tools-reference/).

When Envoy starts, it generates a `CLAUDE.md` file in your project root with TD development patterns, the complete MCP tool reference, and project-specific guidance.

---

## TDN Network Format

TDN (TouchDesigner Network) is a JSON-based format for exporting operator networks as human-readable, diffable text. Unlike binary `.toe` and `.tox` files, `.tdn` files can be meaningfully diffed in git.

- **Entire project**: `ctrl + shift + e`
- **Current COMP**: `ctrl + alt + e`
- **Via Envoy**: `export_network` / `import_network` MCP tools

See the [full TDN specification](https://dylanroscover.github.io/Embody/tdn/specification/) for format details, import process, and round-trip guarantees.

---

<details>
<summary><strong>Logging</strong></summary>

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

</details>

<details>
<summary><strong>Testing</strong></summary>

Embody includes **38 test suites** covering core externalization, MCP tools, TDN format, and server lifecycle. Tests run inside TouchDesigner using a custom test runner with sandbox isolation.

```python
op.unit_tests.RunTests()                              # All tests (non-blocking)
op.unit_tests.RunTests(suite_name='test_path_utils')   # Single suite
op.unit_tests.RunTestsSync()                           # All in one frame (blocks TD)
```

Via Envoy MCP: use the `run_tests` tool. See the [full testing docs](https://dylanroscover.github.io/Embody/testing/) for coverage details and how to write new tests.

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

- **Timeline Paused**: Embody requires the timeline to be running. An error appears if paused.
- **Clone/Replicant Operators**: Cannot be externalized. Embody warns if you try to tag them.
- **Engine COMPs**: Engine, time, and annotate COMPs are not supported for externalization.

For more, see [Troubleshooting](https://dylanroscover.github.io/Embody/embody/troubleshooting/).

</details>

---

## Version History

See the [full changelog](https://dylanroscover.github.io/Embody/changelog/) for detailed version history.

**Recent releases:**

- **5.0.235**: `restart_td` meta-tool, local MCP handshake, operator overlap warnings
- **5.0.233**: Project-level performance monitoring, `/validate` command, test runner dialog fix
- **5.0.229**: Warning support in `get_op_errors`, Envoy enable dialog improvement
- **5.0.228**: macOS timezone fix, toolbar hover highlight
- **5.0.227**: TDN crash safety — atomic writes, backup rotation, content-equal skip, About page filtering
- **5.0.222**: Rename `tag_for_externalization` → `externalize_op`, clarify single-step workflow
- **5.0.221**: TDN annotation properties, GitHub release rule, templates cleanup
- **5.0.220**: Network layout rewrite, commit-push checklist rule, expanded MCP tool allowlist, tooltip fix
- **5.0.217**: TDN target COMP parameter preservation, user-prompted file cleanup, dock safety, git init hardening
- **5.0.210**: DAT restoration on startup, continuity check hardening, manager list row limiting
- **5.0.208**: Settings auto-deploy, bridge template, Envoy startup resilience
- **5.0.206**: Metadata reconciliation, network layout tool, TDN companion dedup
- **5.0.204**: Custom window header, path portability, TDN cleanup
- **5.0.201**: Robust first-install init, table schema expansion, release build hardening
- **5.0.190**: Automatic restoration — TOX and TDN strategy COMPs fully restored from disk on project open
- **5.0**: Envoy MCP server (45 tools), TDN format, test framework (38 suites), macOS support

---

## Contributors

Originally derived from [External Tox Saver](https://github.com/franklin113/External-Tox-Saver) by [Tim Franklin](https://github.com/franklin113/). Refactored entirely by Dylan Roscover, with inspiration and guidance from Elburz Sorkhabi, Matthew Ragan and Wieland Hilker.

## License

[TEC Friendly License v1.0](LICENSE)
