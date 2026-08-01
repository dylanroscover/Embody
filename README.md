<div align="center">

<img src="docs/assets/embody-mark.svg" alt="Embody" width="96" height="96">

# Embody

**create at the speed of thought.**

[![Version](https://img.shields.io/badge/version-6.0.171-6ee668?style=flat-square&labelColor=181e1e)](https://github.com/dylanroscover/Embody/releases/latest)
[![TouchDesigner](https://img.shields.io/badge/TouchDesigner-2025-6ee668?style=flat-square&labelColor=181e1e)](https://derivative.ca/)
[![MCP Tools](https://img.shields.io/badge/MCP_tools-60-6ee668?style=flat-square&labelColor=181e1e)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-6ee668?style=flat-square&labelColor=181e1e)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/dylanroscover/Embody?style=flat-square&labelColor=181e1e&color=6ee668)](https://github.com/dylanroscover/Embody/stargazers)
[![Downloads](https://img.shields.io/github/downloads/dylanroscover/Embody/total?style=flat-square&labelColor=181e1e&color=6ee668)](https://github.com/dylanroscover/Embody/releases)

[**embody.tools**](https://embody.tools) &nbsp;&middot;&nbsp; [Documentation](https://dylanroscover.github.io/Embody/) &nbsp;&middot;&nbsp; [Manifesto](https://dylanroscover.github.io/Embody/manifesto/) &nbsp;&middot;&nbsp; [Changelog](https://dylanroscover.github.io/Embody/changelog/)

</div>

---

Embody puts your ideas on screen as fast as you can describe them. Operators, connections, parameters, the works. Want to try a different direction? Spin up a new approach in seconds. Compare attempts side by side. Branch off the one that works. **The tool keeps up with you, instead of the other way around.**

## Three Tools, One Idea

**Envoy** — *forward velocity.* An embedded [MCP](https://modelcontextprotocol.io/) server lets [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), [OpenCode](https://opencode.ai/), [Gemini](https://github.com/google-gemini/gemini-cli), [Cursor](https://www.cursor.com/), [Windsurf](https://windsurf.com/), and [GitHub Copilot](https://github.com/features/copilot) (via VS Code) talk directly to your live TouchDesigner session. Create operators, wire them up, set parameters, write extensions, debug errors — by saying what you want. No copy-pasting code. No describing your network in chat. Idea → operators in seconds.

**Embody** — *lateral velocity.* Tag any operator and Embody externalizes it to files on disk that mirror your network hierarchy. Try a new direction, branch off a good one, restore the state from yesterday — all in seconds. Your externalized files are the source of truth, so every project opens already in flow.

**TDN** — *the substrate that makes both possible.* TouchDesigner networks exported as human-readable YAML. The format is what lets your AI agent understand what's on the screen, what lets you diff one attempt against another, and what lets a network reconstruct itself from text on the next project open. TDN is what makes the rest of this possible.

![Embody Manager UI](docs/assets/embody-screenshot.png)

| | What | Why it matters |
|---|---|---|
| 🤖 | **Envoy MCP Server** | 60 tools let your AI assistant build, wire, parameterize, and debug live networks. The first time you watch it happen, you stop typing operator names by hand for good. |
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

When Envoy starts, it always generates an `AGENTS.md` file in your project root with TD development patterns and project-specific guidance. It also writes a client-specific config for whichever assistant you select in the `Aiclient` parameter (`CLAUDE.md` + `.claude/` for Claude Code, `opencode.json` + `.claude/` for OpenCode, Cursor/Windsurf rules, Copilot instructions, `GEMINI.md` for Gemini; Codex and OpenCode read `AGENTS.md` directly). For OpenCode and local-model setups, see the [Local Models & Open Clients](https://dylanroscover.github.io/Embody/envoy/local-models/) guide.

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

Embody includes **114 test suites** (2,895 tests) covering core externalization, MCP tools, TDN format, the Envoy server/bridge, launch/config generation, install/uninstall paths, self-update, release hooks, and palette catalogs. Tests run inside TouchDesigner using a custom test runner with sandbox isolation. Destructive whole-project suites are segregated and run only via the save-gated `RunDestructiveTests`.

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

- **6.0.171**: **Convoy relays work into TouchDesigner** -- the host app's dispatcher now drains its own queue (a `dispatching` claim state, a compare-and-set so two dispatchers can never double-run a job, and the forward moved OUT of the global lock so a 30 s relay cannot freeze every route), long operations (`run_tests`, `save_project`) relay end to end through node-job polling with the node's own verdict mirrored back, and **TouchDesigner registers itself**: a new Convoy parameter page plus a `ConvoyExt` child COMP that finds the local host app, registers its live Envoy port, heartbeats, and asks once -- naming the convoy id it will mint and the scope it grants -- before writing anything. The **bridge streams**: server-pushed frames and `tools/list_changed` reach the client as they arrive instead of dying in a buffer, and every severed response now answers the client (two shapes were silent hangs in every shipped version). Ten adversarial review rounds across four slices found 40+ probe-reproduced defects. Also fixed: `raise SkipTest` in `setUp` had always been miscounted as an ERROR by the test runner. **2,895 tests** (114 suites).
- **6.0.169**: **Perform Mode no longer fights the Envoy liveness watchdog** — entering Perform Mode stops Envoy but deliberately leaves `Envoyenable` on, and the watchdog read that enabled-but-down state as an outage: it revived the server ~4-12 s into every performance and overwrote the `Perform Mode` status readout with `Reviving (watchdog)...`. Perform Mode is now a first-class watchdog idle condition gated on the live par authority (never the status string), `Start()` refuses mid-Perform, and — from the adversarial review of the fix itself — an in-flight start can no longer cross a Perform entry: all three startup polls honor Perform, tearing down a worker that bound mid-entry instead of declaring `Running` over the show. The thread-exit hooks were unified onto the same authority helper, whose failure mode (errors read False) can never disable self-healing. Also: **TDN files export with LF line endings** (the writer pinned `newline='\n'`, closing the same churn class v6.0.168 fixed for generated rules). Twelve new watchdog tests, including the poll gates and the unstubbed authority wire. **2,516 tests** (112 suites).
- **6.0.168**: **No more phantom git changes from Embody's own generated files** — `Path.write_text()` defaults to `newline=None`, translating line feeds to CRLF on Windows, so `write_template` rewrote every generated rule and skill with CRLF on each deploy while `.gitattributes` declares `*.md eol=lf`. That produced a permanent row of `M` badges with an EMPTY `git diff` (the eol attribute normalizes on compare, so `git status` and `git diff` disagree) — and in a user project lacking `.gitattributes`, CRLF got committed and churned against macOS/Linux collaborators. Ten write sites now pin `newline='\n'` (`write_template`, the CLAUDE.md/ENVOY.md writers, both JSON manifests, the `.gitignore`/`.gitattributes` editors), and the nine already-CRLF files were normalized once. Scoped deliberately to Embody's own writers: 103 tracked files carry CRLF from ordinary Windows editing, but those are inert because nothing regenerates them. **2,504 tests** (112 suites).
- **6.0.166**: **Multi-agent flow** — a **shared task ledger** (`announce_task`/`update_task`, `.embody/tasks.json`) gives parallel AI sessions work-STATE, not just presence: `done_uncommitted` marks finished work sitting uncommitted in the tree (born from a real misread the same morning — a finished feature held another session's batch because nothing shared records completion), active entries ride on `get_sessions`, and `preflight_landing` warns before anyone lands over uncommitted finished work. **Long operations become restart-proof background jobs** (`run_tests background=True`, new `save_project`, `get_job_status`): handles return immediately and results park in `.embody/jobs/`, surviving the server restarts that severed synchronous calls twice in one day. Plus: the write-effect footer stops counting TD's own `/ui` warnings as your damage, and the **fresh-install smoke runs fully headless** (the harness drives the Setup Wizard's backend instead of stalling on its panel). Tools 56 → 60. **2,542 tests** (112 suites).
- **6.0.165**: **Five agent-experience features from a competitor teardown** — a full code-level audit of a rival TD AI plugin exposed five real gaps, closed here without adopting its telemetry, system-file patching, or runtime prompt manipulation. **`get_guidance`** finally serves this project's rules and skills (36 documents) over MCP, so Codex/Cursor/opencode agents get the same TouchDesigner doctrine Claude Code loads instead of only tool schemas. **`get_focus`** reports the pane's network, selection, current op and rollover with an explicit disambiguation rule ("this operator" is the SELECTED op, never the incidental rollover). Mutating tool calls now ride back an **`_effects`** block naming errors and warnings that did NOT exist before the call plus a meaningful fps drop — diffed against a per-session baseline, self-disabling if the scan exceeds its time budget, and reading the Perform CHOP only when it already exists so a write never creates a monitor as a side effect. The **setup wizard now offers externalization** (whole project / new work only / not now), gated to refuse without a saved `.toe` recovery point and routed through the existing `ExternalizeProject()` so its confirmation dialog still applies. And **launching a CLI agent seeds its opening prompt** once per project, positionally (never `-p`, which would answer once and exit), with GUI editors untouched. Adding two tools tripped `test_version_sync`, which caught a README badge a feature-table grep missed. **2,516 tests** (110 suites).
- **6.0.162**: **MCP SDK 2.0** — the SDK's overnight 2.0.0 release removed `mcp.server.fastmcp` and **broke every fresh install** (issue #81: Embody's dep spec had no ceiling, so new venvs resolved 2.0.0 under 1.x code; the import gate checked only `mcp.server`, which 2.0 still provides, so the failure surfaced as a 30-minute restart storm). Envoy now runs on `MCPServer` natively — verified against the real wheel on TD's exact Python: tools, prompts, `Image` captures, `Optional[Literal]` schemas, DNS-rebinding rejection, and old-revision client handshakes all pass, so existing Claude Code sessions keep working. The pin is a range (`mcp>=2.0.0,<3`), and **venvs now self-upgrade**: each install stamps the venv with its dep spec + Python; any release that changes a pin auto-upgrades every existing venv on next start (a TD Python bump rebuilds via `uv venv --clear`), and upgrading a live session refuses with an honest "restart TouchDesigner to finish the upgrade" instead of importing a mixed stack that can abort() TD. Also: 2.0's silent 4 MiB request-body cap raised to 64 MiB (big `import_network` payloads no longer masquerade as lost connections), and its `logging.basicConfig` side effect (root stderr handler at INFO, process-wide) is undone so the textport stays clean. **2,424 tests** (108 suites).
- **6.0.160**: **Two guards that were silently off** — `is_pid_alive` used `OpenProcess` alone, but a Windows process object (and its PID) stays allocated while any handle to it is open, so **exited processes read as alive forever**: dead registry rows were never pruned, the basename was never reclaimed, and every relaunch minted another `-2`/`-3` instance while bridges chased a corpse (stranded heartbeats also lingered as phantom peers). A zero-timeout `WaitForSingleObject` distinguishes signaled-on-exit from running. Separately, the test runner's `_running` was instance state, so editing any `syncfile`'d source mid-run rebuilt the extension and **disarmed dialog suppression** — a real "Embody — Uninstall" modal escaped to the user, and once clicked it stopped the live Envoy server; it is now storage-backed with a refreshed TTL. Six more runtime keys (including `_smoke_test_responses`, which would have auto-answered real modals) stopped leaking into committed `.tdn`, now enforced by an **invariant** test rather than a hand-maintained list. Clipboard suites SKIP loudly on OS clipboard contention instead of blaming the watcher, and the fresh-install smoke waits for a terminal Envoy state and writes an explicit verdict. **2,407 tests** (108 suites).
- **6.0.159**: **Field-report triage** — `remove_externalization_tag` had been a DEAD MCP tool since v6.0.154 (its wrapper always forwarded `delete_file`; the handler took only `op_path`, so every call returned a TypeError) and shipped through two releases green, because no test had ever invoked a registered tool wrapper — now fixed and permanently guarded by a **tool-schema conformance** suite that checks all 54 tools for three directions of wrapper/handler drift. Closed a **silent `.tdn` deletion on every save**: `checkOpsForContinuity` used a bare `op()`, which cannot resolve a utility `annotateCOMP`, so a legacy row at an annotation read as a vanished operator. **TDN sequence export** now discovers via `target.seq` (pars-based discovery misses sequences on an uncooked POP) and can no longer emit an unimportable `name: []`. Plus `.gitignore` duplicate-header consolidation, two silent config migrations that had never run, and a `crash_detected` flag that stuck forever after an external TD relaunch. **2,400 tests** (108 suites).
- **6.0.157**: **Per-session bridge routing** — each session's bridge pins to a TD instance by name (registry churn can't re-target it; a pinned instance's port change still self-heals), `switch_instance` moves only the calling session (`all_sessions=true` + `active_epoch` for the explicit whole-user move), and registration is adopt-if-vacant — a fresh instance can no longer yank every live session's bridge (verified live: second instance registered, five sessions, zero bridges moved). Worktree tasks get **durable claims** (survive session death + Envoy restarts, visible via `get_sessions.worktrees`) and the new **`preflight_landing`** tool checks a worktree diff against main-tree dirt, peer territory, and unsaved TDN state before landing. Undo-block guard self-heals a severed begin/end pair. **2,357 tests** (104 suites).
- **6.0.156**: **AI-guidance context overhaul** — the heaviest always-loaded rules become slim invariants with full recipes relocated into the on-demand skills that load at point of use (`/create-operator` gains the canonical positioning recipe and covers all creation/movement; `/td-api-reference` gains referencing patterns, cook-model gotchas, and a Heavy-Build Safety section), cutting resident context ~50%. Envoy tool docstrings now state their skill prerequisites and 7 parameters became schema-enforced `Literal` enums (documented values unchanged). A 5-reviewer line-level conflict audit fixed 11 drifts across rules/skills (glslMAT docks vertex/pixel/info, time-dependent cook-model, absolute-path examples, TDN YAML wording, `project.dirty` -> `project.modified`, ...). Smoke harness fails loud on locked cleanup. **2,337 tests** (104 suites).
- **6.0.154**: **Large TDN exports get a progress dialog** — `ExportNetworkAsync` (behind the toolbar export button, the export shortcut, and whole-project TDN export) now opens a small centered window for exports of >= 500 operators: title, a live `N / total operators (pct)` status line, a progress bar, and a **Cancel** button (consumed on the next batch boundary — no file written, worker unwinds clean). The work was already batched across frames (now tunable via `batch_size`), so TD stays responsive — verified live at **10,260 operators** (held 60fps) and a 3,060-op content-heavy network that serialized to a 3 MB `.tdn`, where a synchronous export blocks ~1.5s. The completion frame no longer stacks window-teardown + tracking + list-rebuild (post-export drop burst gone). Plus two untag fixes: **TDN untag no longer leaves a ghost row** (`remove_externalization_tag` routes through `RemoveTDNEntry`, clearing the table row + `_tdn_rel_path` breadcrumb the refresh sweep kept resurrecting; new `delete_file` flag, default off), and **`externalize_op` reports the real `.tdn` filename** for `tag_type='tdn'`. **2,337 tests** (104 suites).
- **6.0.153**: **Envoy port scanner survives a zombie TD holding a port** — `_findAvailablePort` now probes candidates with a real `bind()` instead of a TCP `connect()`. A windowless leftover TouchDesigner can hold a port bound + LISTENING with a dead accept loop, so connects are refused (old probe read it "free") while uvicorn's `bind()` still fails with `WinError 10048` — and the retry loop re-elected the same poisoned port for the full 30-min window (observed: a second `.toe` in one repo crash-looped on a port an hours-old dev instance still camped). The bind probe does exactly what uvicorn does, so a dead listener can't fool it (and it drops the 1s connect-timeout per busy port). Plus a 10-minute **bind-failure blacklist** so a probe/bind race advances to the next port instead of looping; a confirmed bind clears it. **2,327 tests** (103 suites).
- **6.0.152**: **Release hooks for Export Portable Tox** (issue #74) — `pre_release` runs on a throwaway staged copy ([Private Investigator](https://github.com/AlphaMoonbaseBerlin/TD_Private_Investigator)'s model: shape the artifact, live comp untouched, hook code never ships), `post_release` runs on the original after the save with the path + success flag; failed pre-hooks keep the staged copy for inspection. **OpenCode is a first-class AI client** — generated `opencode.json` spawns the same STDIO bridge, loads the generated rules, and uninstalls cleanly; new **Local Models & Open Clients** docs page. **Setup wizard asks about git** (initialize or skip — no more silent handling) and lists OpenCode. **`ReleaseAll()`** batch-exports every tracked, hook-bearing component; **Show Built-in Pars** toggle (Advanced) unhides TD's parameter pages. `localhost` → `127.0.0.1` across all shipped/machine surfaces. **2,321 tests** (103 suites).
- **6.0.149**: **Auto-Update controls moved to the About page** — `Autoupdate`/`Checkforupdate`/`Updatestatus` now sit with the version info (section break below Date), returning Advanced to its focused shape. Behavior unchanged. **2,225 tests** (101 suites).
- **6.0.148**: **Custom-pages-only parameter dialog** (the POPX pattern) — `showCustomOnly` on the Embody COMP shows the 9 Embody pages instead of those plus TD's built-in Layout/Panel/Look/... pages (still functional, just filtered); applied in `EmbodyExt.__init__` so existing installs converge after updating. Parameter Reference truth-synced to the component's par help (`Update Status` default is `Disabled`). **2,225 tests** (101 suites).
- **6.0.147**: **Update-available dialog trimmed** — version pair, a release-notes link, Install / Not Now; the embedded 600-char notes body is gone (a yes/no prompt is a decision, not a reading assignment). Renders from the installed updater, so it applies to checks made on v6.0.147+. **2,223 tests** (100 suites).
- **6.0.146**: **`Update Status` is never blank** — it rests at `Disabled` whenever Auto-Update is Off: on a fresh install (v6.0.145 shipped an empty field, which read as broken), on every project open (replacing stale results from sessions that had checks on), and the moment the preference is flipped. Fresh-install `.tox` smoke is now a mandatory release step — the miss that shipped the empty field. **2,223 tests** (100 suites).
- **6.0.145**: **Annotations are never externalized per-op** (external report: all four issues verified, then fixed) — code-created annotations could be swept into bogus per-op TDN/source boundaries whose reconstruction gutted the widget's TD-managed internals (`float(None)` cook errors) and stranded orphan files; tagging now refuses annotates and their interiors at every layer, legacy rows are inert with cold-open re-checks, and `create_annotation` creates **`utility=True`** (TD-UI parity — live-verified that sweeps cannot see utility subtrees). **Every op-path Envoy tool resolves utility annotations** via a shared resolver (~34 tools; `delete_op` no longer says "Operator not found" on a path `get_annotations` just listed), and **annotation deletion is durable** (purge + checkpoint drop the semantic entry; no more resurrection — delete via `delete_op`, never raw `.destroy()`). **Self-update ships**: manifest-gated (`embody-release.json`: sha256/size/`min_td_build`), background download + verify, backup + rollback, settings preserved — `Autoupdate` defaults to Off. Destructive-test save-gate fixed (`project.modified`; `project.dirty` doesn't exist on TD 2025). Adversarial 5-lens review + cold-open restart smoke. **2,220 tests** (100 suites).
- **6.0.141**: **Issue #57's MCP `create_op` freeze fixed at its trigger** — on one reporter's TD 2025.32460 the first mutating call of a session wedged TD's main thread permanently (dump-verified: viz editor work in the same frame as the network mutation); two activation gates now guarantee the MCP response is delivered before any Embot/camera editor work runs, and the first activation after dormancy pings the node colour only (new `test_envoy_viz_gates` suite; adversarial panel: no defects). **TDN locked-content warnings** collapse to one combined dialog with a Don't-show-again preference (`Tdnlockedwarn`). **Dirty badges no longer vanish** after extension source edits (fingerprint cache survives reinit). Manager filter gains a **`dirty` keyword** + force-expand. `.tdn` git diffs are **UTF-8-safe**, and runtime storage (`git_status`, `expand_order`, `_tdn_fingerprints`, `_suppress_dialogs`) stays out of `.tdn` exports. **2,171 tests passing** (98 suites).
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
