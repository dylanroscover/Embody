<div align="center">

<img src="docs/assets/embody-mark.svg" alt="Embody" width="96" height="96">

# Embody

**create at the speed of thought.**

[![Version](https://img.shields.io/badge/version-6.0.138-6ee668?style=flat-square&labelColor=181e1e)](https://github.com/dylanroscover/Embody/releases/latest)
[![TouchDesigner](https://img.shields.io/badge/TouchDesigner-2025-6ee668?style=flat-square&labelColor=181e1e)](https://derivative.ca/)
[![MCP Tools](https://img.shields.io/badge/MCP_tools-53-6ee668?style=flat-square&labelColor=181e1e)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-6ee668?style=flat-square&labelColor=181e1e)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/dylanroscover/Embody?style=flat-square&labelColor=181e1e&color=6ee668)](https://github.com/dylanroscover/Embody/stargazers)
[![Downloads](https://img.shields.io/github/downloads/dylanroscover/Embody/total?style=flat-square&labelColor=181e1e&color=6ee668)](https://github.com/dylanroscover/Embody/releases)

[**embody.tools**](https://embody.tools) &nbsp;&middot;&nbsp; [Documentation](https://dylanroscover.github.io/Embody/) &nbsp;&middot;&nbsp; [Manifesto](https://dylanroscover.github.io/Embody/manifesto/) &nbsp;&middot;&nbsp; [Changelog](https://dylanroscover.github.io/Embody/changelog/)

</div>

---

Embody puts your ideas on screen as fast as you can describe them. Operators, connections, parameters, the works. Want to try a different direction? Spin up a new approach in seconds. Compare attempts side by side. Branch off the one that works. **The tool keeps up with you, instead of the other way around.**

## Three Tools, One Idea

**Envoy** — *forward velocity.* An embedded [MCP](https://modelcontextprotocol.io/) server lets [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), [Gemini](https://github.com/google-gemini/gemini-cli), [Cursor](https://www.cursor.com/), [Windsurf](https://windsurf.com/), and [GitHub Copilot](https://github.com/features/copilot) (via VS Code) talk directly to your live TouchDesigner session. Create operators, wire them up, set parameters, write extensions, debug errors — by saying what you want. No copy-pasting code. No describing your network in chat. Idea → operators in seconds.

**Embody** — *lateral velocity.* Tag any operator and Embody externalizes it to files on disk that mirror your network hierarchy. Try a new direction, branch off a good one, restore the state from yesterday — all in seconds. Your externalized files are the source of truth, so every project opens already in flow.

**TDN** — *the substrate that makes both possible.* TouchDesigner networks exported as human-readable YAML. The format is what lets your AI agent understand what's on the screen, what lets you diff one attempt against another, and what lets a network reconstruct itself from text on the next project open. TDN is what makes the rest of this possible.

![Embody Manager UI](docs/assets/embody-screenshot.png)

| | What | Why it matters |
|---|---|---|
| 🤖 | **Envoy MCP Server** | 53 tools let your AI assistant build, wire, parameterize, and debug live networks. The first time you watch it happen, you stop typing operator names by hand for good. |
| 📄 | **TDN Network Format** | Networks become text. Diff two versions, revisit any version, hand an LLM a complete picture of what's on screen — all from a single `.tdn` file. |
| 📦 | **Automatic Restoration** | Externalized files are written on save, so any COMP can be recovered from disk. By default (Export-on-Save) the `.toe` stays authoritative on open; switch to Roundtrip mode to rebuild TDN-strategy COMPs from `.tdn` on every open. |
| 📤 | **Portable Tox Export** | Pull any COMP out as a self-contained `.tox` with external references stripped. Ship a piece of your project anywhere. |

---

## Quick Start

**Requirements:** TouchDesigner **2025.33070 or later** (Windows / macOS). No Python setup needed — Envoy installs its own dependencies on first enable. No special folder structure either: Embody works in any project folder, and if you happen to use git, every change is also a clean diff for free.

### 1. Install

**Download** the Embody `.tox` from [`/release`](release/) and drag it into your TouchDesigner project. The **[Setup Wizard](https://dylanroscover.github.io/Embody/embody/setup-wizard/)** opens and walks you through the choices that matter — how much autonomy Embody gets, whether to enable the AI assistant (Envoy) and for which tool, permissions, and where config files live. Nothing changes until the final click, and you can re-run it anytime via the **Setup Wizard** pulse on the Embody COMP.

> **Updating Embody:** delete the old Embody COMP and drag the new `.tox` in its place. Your settings and tracked externalizations live on disk, so the new version picks them up automatically and quietly validates everything it's tracking — no re-scan, no dialogs, no files rewritten.

### 2. Tag and Work

1. **Tag operators** — hover any COMP or DAT and press `lctrl` twice to open the tagger (pick a strategy for a COMP, a file format for a DAT)
2. **Work normally** — press `ctrl + shift + u` to update all externalizations, or `ctrl + alt + u` to update only the current COMP. Externalized files are written on save; on open, the `.toe` stays authoritative by default (Export-on-Save), while Roundtrip mode also reconstructs TDN-strategy COMPs from disk

> **Tip:** Externalization is opt-in — nothing is written to disk until you tag it. To capture your AI assistant's work automatically, set **Auto-Externalize New Ops** (Envoy parameter page) and everything it creates through Envoy is tagged and externalized as it's built.

For supported formats, folder configuration, duplicate handling, Manager UI, and more — see the [Embody docs](https://dylanroscover.github.io/Embody/embody/).

---

## Envoy MCP Server

Embody includes **Envoy**, an embedded [MCP](https://modelcontextprotocol.io/) server that gives AI coding assistants direct access to your live TouchDesigner session.

### Setup

1. **Pick an AI assistant in the [Setup Wizard](https://dylanroscover.github.io/Embody/embody/setup-wizard/)** — it opens on first install, or re-run it anytime (the **Setup Wizard** pulse on the Embody COMP). Prefer parameters? Toggling **Envoy Enable** (`Envoyenable`) does the same thing with your current settings
2. **Server starts** on `127.0.0.1:9870` (configurable via `Envoyport`; if the port is taken by another instance, Envoy scans forward automatically)
3. **Auto-configuration** — Envoy writes a `.mcp.json` (STDIO bridge, so tools are available even before TD is running) at your AI project root. By default that's the git repo root; the wizard's config-location step — or the `Aiprojectroot` parameter — can point it at the `.toe` folder or a custom path instead. Projects without a git repo still get config generated in the `.toe` folder
4. **Connect** — open a Claude Code session (or restart your IDE) at that root — it picks up `.mcp.json` automatically

The generated config runs Envoy's bridged STDIO transport (recommended — it can launch and restart TD for you). If you'd rather wire a client by hand, the direct HTTP transport works whenever TD is running:

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

### Tools at a Glance

| Tool | What It Does |
|------|-------------|
| `create_op` | Create any operator type in any network |
| `set_parameter` | Set values, expressions, or bind modes on any parameter |
| `connect_ops` | Wire operators together |
| `execute_python` | Run arbitrary Python in TD's main thread |
| `export_network` | Export networks to diffable `.tdn` YAML |
| `create_extension` | Scaffold a full extension (COMP + DAT + wiring) |
| `get_op_errors` | Inspect errors on any operator and its children |

...and 46 more. See the [full tools reference](https://dylanroscover.github.io/Embody/envoy/tools-reference/).

When Envoy starts, it always generates an `AGENTS.md` file in your project root with TD development patterns and project-specific guidance. It also writes a client-specific config for whichever assistant you select in the `Aiclient` parameter (`CLAUDE.md` + `.claude/` for Claude Code, Cursor/Windsurf rules, Copilot instructions, `GEMINI.md` for Gemini; Codex reads `AGENTS.md` directly).

---

## TDN Network Format

TDN (TouchDesigner Network) is the file format that makes the rest of Embody possible. It exports an entire operator network — operators, connections, parameters, layout, annotations, DAT content — as a single human-readable YAML file. Your AI agent can read it. You can read it. Any text tool can diff it. The network can rebuild itself from it.

This is the substrate. Every other capability — AI-driven building, version control, automatic restoration — builds on top of it.

- **Entire project**: `ctrl + shift + e`
- **Current COMP**: `ctrl + alt + e`
- **Via Envoy**: `export_network` / `import_network` MCP tools

See the [full TDN specification](https://dylanroscover.github.io/Embody/tdn/specification/) for format details, import process, and round-trip guarantees.

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `lctrl + lctrl` | Tag or manage the operator under the cursor |
| `ctrl + shift + u` | Update all externalizations |
| `ctrl + alt + u` | Update only the current COMP |
| `ctrl + shift + r` | Refresh tracking state |
| `ctrl + shift + o` | Open the Manager UI |
| `ctrl + shift + c` | Copy the selected COMP to the clipboard as a portable TDN envelope |
| `ctrl + shift + e` | Export entire project to `.tdn` file |
| `ctrl + alt + e` | Export current COMP to `.tdn` file |

These are the defaults — every shortcut is editable on the Embody COMP's **Shortcuts** parameter page (type a combo, or pulse **Record** and press the keys; empty disables it). See [Keyboard Shortcuts](https://dylanroscover.github.io/Embody/embody/keyboard-shortcuts/).

---

<details>
<summary><strong>Where externalized files go</strong></summary>

Embody writes externalized files relative to your `.toe` location, mirroring your network hierarchy — no special folder structure required:

```
my-project/              ← project folder (optionally a git repo)
├── my-project.toe       ← your TouchDesigner project
├── base1/               ← externalized operators
│   ├── base2.tox        ← COMP (TOX strategy)
│   ├── base3.tdn        ← COMP (TDN strategy — diffable YAML)
│   └── text1.py         ← DAT
└── ...
```

</details>

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

Embody includes **97 test suites** (2,135 tests) covering core externalization, MCP tools, TDN format, the Envoy server/bridge, launch/config generation, install/uninstall paths, and palette catalogs. Tests run inside TouchDesigner using a custom test runner with sandbox isolation. Destructive whole-project suites are segregated and run only via the save-gated `RunDestructiveTests`.

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

- **6.0.138**: New shipped skill **`/brief`** — a task-brief compiler: `/brief <conversational request>` turns plain English into a reviewable contract in `briefs/` (the skills to load, live-discovered anchors, verifiable success criteria, performance/multi-session/worktree gates) that the work then executes from — portable to sub-agents and fresh sessions; ships to user projects as the 14th skill, with a Task Briefs section in the generated `CLAUDE.md`. **Launch AI Client** now walks through a missing CLI's install in the opened terminal — the official per-OS command on its own copy/paste line, shell-correct for zsh and cmd.exe (`test_launch_aiclient` 29 → 42). New sync guard: every template-map entry must resolve to a live, non-empty template DAT (a silent-shipping gap caught in review). **2,142 tests passing** (93 suites).
- **6.0.136**: TD 2025 **external-tox reload triggers fixed** — `reloadtoxpulse` does not exist on TD 2025 (the reconcile pass aborted on `tdAttributeError`), toggling `enableexternaltox` off→on does not re-read the `.tox` (manager "Reload from disk" was a silent no-op), and setting `externaltox` mid-session does not auto-load (`RestoreTOXComps` restored **empty shells**). All three paths now pulse `enableexternaltoxpulse` (verified empirically on 2025.32820 + 2025.33070), restores **fail loud** via `externalTimeStamp` (a dead shell is destroyed, never silently kept where a later save could export it empty), and `ReconcileMetadata` guards each row. Root-caused during the stale-tox-restore investigation, which established that on TD 2025 the externalized **file wins** over tox-embedded DAT snapshots in every load path. New `TestTOXRestoration` suite (6 tests); fresh-install smoke-tested from the shipped `.tox`. **2,123 tests passing** (97 suites).
- **6.0.135**: The upgrade **Skip/Re-scan dialog is gone** — dropping a new `.tox` into an existing project now **validates tracked operators quietly** (schema migration, path normalization, per-row continuity, dirty-only re-export) instead of the old "Re-scan", which deleted every tracked file and re-exported the whole project in one synchronous frame — a minutes-long freeze on large projects with a crash window of zero files on disk. A full rebuild stays available via Disable → Enable, which discloses the deletion. Minimum TD build is now **2025.33070**. New `test_verify_upgrade` regression suite; **2,122 tests passing** (97 suites).
- **6.0.134**: TD 2025.33070 first-launch **palette-scan freeze** (loading `geoPanel.tox`/`chromaKey.tox` can wedge the new build's frame loop within a frame of `loadTox` returning -- a TD-side race, reproduced with no Embody code and reported upstream) fixed **structurally**: the scan no longer loads components into TD at all -- a background worker runs TD's bundled `toeexpand` per palette `.tox` and reads type + child count from the expansion (**zero frame drops**; the old path blew the 60fps budget on 78 of its first 91 loads); **33070 bootstrap rows ship pre-baked** (267 components -- current installs never scan); a **freeze sentinel** convicts and skips any future wedge-causing component after one relaunch instead of freeze-looping; legacy loadTox scan is fallback-only, hardened with `allowCooking=False` + blocklist. A save-wedge regression in the sentinel's first iteration (teardown cross-extension call during `ExportPortableTox`'s strip-triggered reinit) was caught and fixed pre-ship. **2,117+ tests passing** (19 new).
- **6.0.131**: Issue [#57](https://github.com/dylanroscover/Embody/issues/57) (Windows MCP transport) -- the STDIO bridge and the HTTP-fallback config target **`127.0.0.1` instead of `localhost`** (Windows resolves `localhost` to `::1` first while Envoy binds IPv4-only; on firewalls that stealth-drop loopback SYNs every MCP call burned ~2s and a full drop became the reported multi-minute `create_op` hang -- measured 2.1s -> 0.07-0.27s per call after the fix); Envoy no longer **restart-storms** when its base port is held by another TD instance (observed 575-attempt loop: generation-stamped restart scheduling, in-flight-start guards, ownership-checked force-close, loud dead-on-arrival diagnostics); bridge **liveness is instance-aware** -- the active instance's image-verified registered PID or its answering port, never "any TouchDesigner process exists" -- and `restart_td` can no longer quit a *different* project's TD on multi-instance machines; `delete_op` purges tracking rows and files for **every** strategy (clone/shared-file guarded); TDN **renames no longer leak the old `.tdn` on Windows** (`Path.replace` overwrite parity); bridge `tools/list` augmentation is idempotent (template/fallback drift healed); launch scripts emit forward-slash paths on every platform. Full Windows suite green for the first time: **2,085 passed / 0 failed** (7 platform skips). Fresh-install smoke-tested from the shipped `.tox`. **92 suites / 2,092 tests**.
- **6.0.128**: Issue [#60](https://github.com/dylanroscover/Embody/issues/60) (Embody in a default startup file) -- the first-launch **palette catalog scan** no longer un-pauses a timeline the user paused mid-scan (per-chunk snapshot bracket), **checkpoints every 25 components and resumes** on the next launch instead of restarting from zero when TD is closed mid-scan (atomic writes; can't wedge, can't re-enable a Disabled Embody); the **"Dropped .tox Expression Detected"** sweep and Externalize Full Project now honor `tdn_exclude` ancestry-wide, plain Ignore holds for the session, and `Toxdropexpr` persists so "Always" answers survive new untitled projects (Envoy opt-in honors restored config the same way); the **venv probe** runs once per session per venv path and a timeout no longer deletes a healthy venv. New shipped rule: **worktree-td-safety**. **92 suites / 2,090+ tests**.
---

## Contributors

Originally derived from [External Tox Saver](https://github.com/franklin113/External-Tox-Saver) by [Tim Franklin](https://github.com/franklin113/). Refactored entirely by Dylan Roscover, with inspiration and guidance from Elburz Sorkhabi, Matthew Ragan and Wieland Hilker.

Want to help? Start with [CONTRIBUTING.md](CONTRIBUTING.md) — this repo works differently from a typical Python project (TouchDesigner writes many of the files), and that page explains what is safe to change and how to run the tests.

## License

[MIT License](LICENSE)
