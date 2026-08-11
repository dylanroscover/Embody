# Testing

Embody includes a comprehensive automated test suite with **120 test suites** and **3,734 test methods** covering core externalization, MCP tools, TDN format, the community/Collection safe-import path, the auto-save checkpoint engine, Envoy server/session coordination, launch/config generation, install/uninstall paths, and palette catalogs. Tests run inside TouchDesigner using a custom test runner with sandbox isolation.

## Running Tests

### From TouchDesigner

```python
# All tests, one per frame (non-blocking, default)
op.unit_tests.RunTests()

# Single suite
op.unit_tests.RunTests(suite_name='test_path_utils')

# Single test method
op.unit_tests.RunTests(suite_name='test_path_utils', test_name='test_normalizePath_backslashes_converted')

# All in one frame (blocks TD until complete)
op.unit_tests.RunTestsSync()

# One suite per frame
op.unit_tests.RunTestsDeferred()

# Get results
results = op.unit_tests.GetResults()
# Example after a full successful run:
# {'total': 2080, 'passed': 2080, 'failed': 0, 'errors': 0, 'skipped': 0, 'results': [...]}
```

### Via MCP

Use the `run_tests` Envoy tool:

```
run_tests()                              # Run all suites
run_tests(suite_name='test_path_utils')  # Run one suite
```

## Test Coverage

### Core Embody (36 suites, 701 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_startup_progress` | 111 | The status readout's signals: the state each subsystem's status string maps to (including the idle states that used to read as work in progress), the age an auto-save stamp becomes, the elapsed clock a wedged step must show, and the rows `viz_status` renders -- including the predicate that decides whether the panel may re-arm a tick at all, and the mutation guard over the removed publish machinery. Pure module, so it also runs under plain pytest on the CI matrix |
| `test_duplicate_handling` | 43 | Duplicate / clone / replicant resolution |
| `test_custom_parameters` | 32 | Custom parameter behavior (Folder, Disable/Enable, Update, TDN controls, Logs, Envoy) |
| `test_tag_management` | 29 | Tagging operators for externalization |
| `test_rename_move_lifecycle` | 26 | Rename and move tracking |
| `test_crud_operators` | 21 | Create, read, update, delete operations |
| `test_git_status` | 23 | Git status / uncommitted-file detection |
| `test_param_tracker` | 20 | Parameter change tracking |
| `test_v6_hardening` | 22 | v6 community-paste + strip/restore hardening |
| `test_ancestor_rename` | 19 | Ancestor-rename detection and folder migration |
| `test_path_utils` | 20 | Path normalization and utilities |
| `test_autosave` | 27 | Auto-save checkpoint engine (skip_cleanup, idle-settle drain, crash recovery, gates) |
| `test_tag_lifecycle` | 18 | Tag application and removal |
| `test_delete_cleanup` | 17 | Deletion and file cleanup |
| `test_auto_externalize` | 16 | Auto-externalization flow and eligible operator handling |
| `test_shortcuts` | 48 | Editable keyboard shortcuts: combo normalization, dispatch, TD reserved-list parsing, duplicate blocking, recorder state machine, parexec handlers, persistence whitelist |
| `test_strategy_handlers` | 22 | TOX/TDN strategy switch, remove, DAT convert |
| `test_issue21_safe_cell` | 14 | Safe table-cell handling |
| `test_glsl_externalize` | 11 | GLSL shader auto-externalization |
| `test_setup_wizard` | 21 | Setup Wizard flow and first-run prompts |
| `test_toxdrop_expr` | 15 | Dropped `.tox` expression cleanup choices |
| `test_layout_lint` | 16 | `execute_python` layout-warning linting |
| `test_logging` | 10 | Logging system and ring buffer |
| `test_advanced_guard` | 9 | Advanced-mode and guarded operation behavior |
| `test_externalization` | 9 | Externalization lifecycle |
| `test_file_management` | 8 | File I/O, path handling, tracked-file delete safety |
| `test_os_label` | 8 | OS label resolution (Win10/11 build thresholds, macOS) |
| `test_dialog_suppression` | 8 | File-cleanup dialog suppression during tests |
| `test_update_sync` | 7 | Sync between .toe and externalized files |
| `test_operator_queries` | 6 | Operator discovery and queries |
| `test_embody_mode_guard` | 5 | Mode guard behavior around Embody operations |
| `test_settings_persistence` | 4 | Settings serialization (byte-stable, sorted keys) |
| `test_annotation_guards` | 20 | Annotations and their internals are never tagged, externalized, tracked, or enumerated as per-op boundaries |
| `test_component_presentation` | 3 | Shipped-component presentation invariants |
| `test_verify_upgrade` | 3 | Upgrade-path validation (the removed Skip/Re-scan dialog) |
| `test_annotate_continuity` | 7 | Continuity sweep vs utility `annotateCOMP` rows: bare `op()` cannot resolve a utility annotate, so a legacy row at one read as a vanished operator and had its row + `.tdn` deleted on every save |

### MCP Tools (20 suites, 327 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_envoy_tool_guards` | 35 | Envoy tool safety guards (undo blocks, parameter guards/search, rollback, docs, sample grid) |
| `test_recovery_hints` | 15 | Recovery-hint match table vs. real error strings + the additive `_attachRecoveryHints` decorator |
| `test_mcp_externalization` | 24 | Embody integration via MCP (tag, save, status) |
| `test_mcp_operators` | 20 | Create, delete, copy, rename, query, find |
| `test_mcp_annotations` | 31 | Creating and managing annotations |
| `test_mcp_dat_content` | 19 | DAT text/table ops + surgical `edit_dat_content` + wipe guards |
| `test_mcp_diagnostics` | 16 | Error checking, class introspection, module help, log retrieval |
| `test_mcp_flags_position` | 16 | Operator flags, positioning, and `get_network_layout` |
| `test_mcp_project_performance` | 14 | Project-level FPS, memory, hotspots |
| `test_mcp_parameters` | 11 | Get/set parameters, modes, expressions |
| `test_mcp_top_capture` | 15 | TOP image capture (format, quality, resolution) + black/flat/transparent quality verdict |
| `test_mcp_tdn_tools` | 10 | `read_tdn`, `export_network`/`import_network` round-trip |
| `test_mcp_batch` | 9 | Batched multi-operation requests |
| `test_mcp_connections` | 8 | Wiring operators together |
| `test_mcp_code_execution` | 7 | Executing Python in TD |
| `test_mcp_extensions` | 6 | Extension creation and setup |
| `test_mcp_performance` | 5 | Per-operator performance monitoring |
| `test_tool_permissions` | 15 | Tool-permissions posture writer (EnvoyExt) |
| `test_envoy_viz_gates` | 43 | Issue-57 viz activation gates in `envoy_viz` |
| `test_envoy_tool_schema` | 8 | Tool-wrapper/handler signature conformance across every registered MCP tool (forwarded-but-unaccepted, required-but-unforwarded, advertised-but-ignored, duplicate dispatch). Static AST analysis -- invokes no tools |

### TDN Format (20 suites, 695 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_tdn_reconstruction` | 208 | Reconstruction round-trip fidelity |
| `test_tdn_file_io` | 92 | TDN file output, per-comp splitting, stale cleanup, tdn_ref / tox_ref pointers |
| `test_tdn_helpers` | 67 | TDN serialization utility functions |
| `test_tdn_export_import` | 48 | Network export/import + storage round-trip |
| `test_tdn_crash_safety` | 35 | Atomic writes, backup rotation, validation |
| `test_tdn_sequences` | 27 | Parameter / operator sequence round-trip |
| `test_tdn_diff_engine` | 25 | TDN structural diff engine |
| `test_tdn_palette_catalog` | 34 | Palette-clone detection and handling |
| `test_tdn_exclude` | 21 | `tdn_exclude` tag (app-managed subtree invisibility) |
| `test_tdn_stability_hardening` | 21 | Import validation, DAT editability capture, flag defaults, stale cleanup, orphan shell recovery |
| `test_tdn_mode` | 15 | Tdnmode gating (off / export / full) |
| `test_dat_restoration` | 20 | DAT restoration from disk on startup |
| `test_tdn_safety_guards` | 14 | At-risk storage / callback-DAT protection |
| `test_tdn_yaml` | 14 | TDN v2.0 YAML emitter / parser |
| `test_tdn_diff` | 11 | `diff_tdn` tool (live-vs-disk, project-wide) |
| `test_tdn_fingerprint` | 15 | Param-aware dirty detection (fingerprint) |
| `test_tdn_annotation_export` | 10 | Annotation-only `.tdn` export (annotateCOMP not double-captured) |
| `test_tdn_external_connections` | 6 | External wire capture/restore across strip |
| `test_tdn_export_progress` | 6 | Chunked TDN export progress dialog + cancellation (and that it leaves no transient state behind) |
| `test_tdn_roundtrip_invariant` | 6 | Writer/reader contract: the exporter must never emit a document its own importer rejects (no empty sequence lists, no sequence silently dropped from an uncooked POP) |

### Community & Collection (6 suites, 123 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_clipboard_paste` | 42 | Clipboard auto-paste import flow |
| `test_collection_scanner` | 22 | Capability scanner verdicts (clean / flagged / blocked) |
| `test_specimen_publish` | 19 | Specimen publish hook |
| `test_collection_safe_import` | 18 | Safe-import `make_inert` disarming |
| `test_collection_pure` | 14 | Pure-value-expression preservation (live-if-clean) |
| `test_clipboard_watch` | 8 | Clipboard watcher poll + gating (incl. outbound-copy suppression) |

### Envoy Server & Bridge (16 suites, 919 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_envoy_bridge` | 428 | STDIO bridge: forwarding, reconciler, registry, meta-tools, and `crash_detected` lifecycle TRANSITIONS (reconnect clears, pid re-resolution honours the session pin, a foreign instance on the port never clears a real crash) |
| `test_claude_config` | 84 | AI client config generation (Claude/Codex/Gemini/Cursor/Windsurf/GitHub Copilot) |
| `test_envoy_sessions` | 55 | Multi-session awareness, scope claims, peer advisories, destructive-operation gates |
| `test_server_lifecycle` | 24 | Envoy MCP server start/stop |
| `test_envoy_watchdog` | 44 | Envoy liveness watchdog (revive on dropped socket / save) |
| `test_version_sync` | 12 | Version badge / minimum-build statements stay in lock-step with `par.Version` and `app.build` |
| `test_envoy_thread_comm` | 20 | Worker/main thread queues and throttling |
| `test_launch_aiclient` | 54 | Launch AI Client launcher (launch table with per-OS install specs, CLI resolution, `.command`/`.bat` generation, missing-CLI install guards and their shell escaping, failure dialogs, env sanitization) |
| `test_envoy_setup_environment` | 44 | MCP import verification (pydantic_core safety) |
| `test_envoy_registry` | 18 | Instance registry and PID liveness |
| `test_opencode_config` | 17 | OpenCode client config writer (`envoy_setup.write_opencode_config`) |
| `test_agent_runner` | 15 | AGENT-tier runner machinery in TestRunnerExt (the runner itself, no LLM) |
| `test_envoy_lifecycle_hardening` | 4 | Save/reinit lifecycle hardening |
| `test_envoy_agent_ux` | 60 | Agent-ergonomics surface: `get_guidance`, the write-effect footer, `get_focus` |
| `test_job_layer` | 25 | Background-job layer backing `get_job_status` / `save_project` / `run_tests background=True` -- the operations that outlive the 30 s MCP timeout |
| `test_task_ledger` | 15 | Shared task ledger (`.embody/tasks.json`): work-STATE across AI sessions, including `done_uncommitted` |

### Convoy (8 suites, 713 tests)

The LAN work relay: the node-side reconciler, the host-app client and installer, and the panel contracts. The host app itself is stdlib-only by design, so most of this tier also runs under plain pytest on the windows+macos CI matrix -- see `pytest.ini`.

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_convoy_install` | 260 | Host-app installer: Scheduled Task XML, LaunchAgent plist, the launcher, and the per-user daemon venv preference |
| `test_convoy_client` | 175 | Host-app client, and its PARITY with the daemon-side probe the two copies must not drift from |
| `test_convoy_host_install` | 105 | ConvoyExt's host-app orchestration (install plan) |
| `test_convoy_ext` | 73 | ConvoyExt, the node-side Convoy reconciler |
| `test_convoy_parameter_contract` | 33 | Off-TD contract for Convoy's user-facing parameter scaffold |
| `test_convoy_host_ladder` | 47 | The runtime ladder that decides WHICH interpreter the daemon runs under, and the register-success revive that replaces a stale down-claiming host line |
| `test_wizard_convoy_contract` | 20 | Plain-Python contracts for Convoy's setup-wizard routing |
| `test_convoy_forget_ux` | 5 | The Forget Offline Nodes daemon -> panel contract |

### Install, Uninstall & Release (13 suites, 248 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_smoke_release` | 34 | Release smoke checks (extensions loaded, Envoy state, Uninstall pulse/handler shipped) |
| `test_install_manifest` | 12 | Generated install manifest and packaged config coverage |
| `test_uninstall_execute` | 11 | Uninstall execution path and cleanup safety |
| `test_uninstall_preview` | 10 | Uninstall preview plan and protected-file handling |
| `test_uninstall_handler` | 5 | Uninstall pulse confirm gate (cancel/suppress/confirm/review) |
| `test_catalog_bootstrap_palette` | 10 | Bootstrap palette table parsing + build coverage |
| `test_catalog_palette_scan` | 39 | Palette scan time-state snapshot/restore |
| `test_template_sync` | 6 | Template map, disk, release-table, and orphan allowlist sync |
| `test_release_hooks` | 52 | `ExportPortableTox` release hooks (issue #74) |
| `test_updater` | 20 | UpdaterExt self-update logic (no network, no swap) |
| `test_config_migration` | 24 | Repo-config writers across a VERSION BUMP -- the migration axis a single-run test cannot see (duplicate managed headers, block consolidation that never swallows user content, `.gitattributes` backfill, the order-dependent `.embody/*` / `!.embody/project.json` pair) |
| `test_wizard_externalize` | 20 | The setup wizard's externalize step, its recovery point, and the already-externalized detection |
| `test_dialog_wrap` | 5 | Every dialog's prose wraps to readable lines (`ui.messageBox` sizes itself to its longest line) |

## Execution Modes

| Mode | Method | Behavior |
|------|--------|----------|
| Per-test deferred | `RunTests()` | One test per frame. Best for heavy suites. Non-blocking. **Default.** |
| Per-suite deferred | `RunTestsDeferred()` | One suite per frame. Keeps TD responsive. |
| Synchronous | `RunTestsSync()` | All tests in one frame. Blocks TD. Use for MCP. |
| Destructive batch | `RunDestructiveTests(confirm_saved=True)` | Save-gated, isolated run of `DESTRUCTIVE` suites only. |
| Agent tier | `RunAgentTests()` | Opt-in, async run of `AGENT` suites only (AI-client subprocesses). |

## Test Tiers

Suites are segregated into three tiers by class attribute. Normal runs
(`RunTests` and friends) NEVER pick up the tagged tiers.

| Tier | Tag | Entry point | What it is |
|------|-----|-------------|------------|
| Normal | (none) | `RunTests()` | Everything above: fast, safe, sandboxed. |
| Destructive | `DESTRUCTIVE = True` | `RunDestructiveTests(confirm_saved=True)` | Whole-project mutators (Disable / ExternalizeProject / Reset). Save first; reopen the saved `.toe` after. |
| Agent | `AGENT = True` (via `AgentTestCase`) | `RunAgentTests()` | External AI clients driving Envoy over MCP (below). |

### Agent Tier (AI-client connectivity tests)

Two layers verify that AI clients can actually reach and use Envoy's MCP
tools, end to end:

| Suite | Layer | What it proves |
|-------|-------|----------------|
| `test_agent_contract` | Tier 1 - deterministic, no LLM | Spawns the exact bridge command from `.mcp.json` via a stdlib MCP client (`agent_clients/mcp_contract_client.py`): handshake, full tool inventory vs manifest, create/write/read-back/batch/delete round-trip. |
| `test_agent_smoke_claude` | Tier 2 - Claude Code headless | `claude -p` (subscription auth) discovers and correctly uses Envoy tools on scripted micro-tasks; verified against live TD state. |
| `test_agent_smoke_codex` | Tier 2 - Codex CLI | `codex exec` with an inline `-c mcp_servers.envoy.*` config (Codex does not read `.mcp.json`); auth-gated by `codex login status`. |
| `test_agent_runner` | Normal tier | Unit tests for the async agent-runner machinery itself (gating, job lifecycle, timeout kill, verdicts). Runs in every normal pass. |

Prerequisites and behavior:

- **CLIs + login**: `claude` and/or `codex` must be installed and logged in on
  this machine. A missing CLI or failed `codex login status` reports a loud
  **SKIP**, never a silent pass.
- **Subscription usage, zero API billing**: the child environment strips
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `CODEX_API_KEY`, so headless runs
  use the stored Pro/Max (Claude) or ChatGPT (Codex) login.
- **Async by design**: `RunAgentTests()` returns immediately and polls each
  subprocess across frames -- MCP requests drain on TD's main thread, so a
  blocking runner would deadlock the tools under test. Poll `GetResults()`
  or watch the results DAT; a full run takes minutes.
- **Verdicts come from primary evidence**: live TD state (the ops the agent
  created, with exact token content) plus structured CLI output -- never the
  agent's prose alone.
- When the Envoy tool surface changes, update `EXPECTED_ENVOY_TOOLS` in
  `test_agent_contract.py` deliberately; the inventory check fails on drift
  in either direction. (The live server only re-registers tools on an Envoy
  restart -- a mismatch right after editing `EnvoyExt.py` means "restart
  Envoy" first.)

See `.claude/skills/agent-tests/SKILL.md` for the full conventions.

## Test Framework Features

- **Sandbox isolation**: Each suite gets a fresh `baseCOMP` for test fixtures
- **unittest-based**: Supports all assertions, lifecycle hooks and other features unittest supports
- **Results tracking**: Table DAT with pass/fail/error/skip counts and durations

## Writing New Tests

Create a test file in the unit tests directory:

```python
"""Test suite: description of what this tests."""

# Base class is auto-injected by the test runner
class TestMyFeature(EmbodyTestCase):

    def test_something(self):
        """Test description."""
        # Create test fixtures in self.sandbox
        op = self.sandbox.create(baseCOMP, 'test_op')

        # Access Embody extension
        result = self.embody_ext.someMethod(op)

        # Assertions
        self.assertEqual(result, expected_value)
        self.assertTrue(op.valid)
        self.assertIn('foo', result)

    def setUp(self):
        """Called before each test (optional)."""
        pass

    def tearDown(self):
        """Called after each test (auto-destroys sandbox children)."""
        super().tearDown()
```

### Available Objects

| Object | Description |
|--------|-------------|
| `self.sandbox` | baseCOMP for creating temporary operators |
| `self.embody` | Reference to `op.Embody` |
| `self.embody_ext` | Direct access to `op.Embody.ext.Embody` |
| `self.runner` | TestRunnerExt instance |
| `op`, `parent`, `root` | All TD globals available |

## What Cannot Be Unit Tested

Some areas require manual testing:

- UI interactions (clicking, dragging, network editor)
- Cross-session persistence (requires closing/reopening `.toe`)
- Keyboard shortcuts (actual key press detection)
- Modal dialogs (file pickers, prompts)
- Undo/redo behavior
- Graphics rendering (visual output validation)
- Real-time performance (sustained load, frame-rate stability)
- External hardware (MIDI, OSC, DMX, serial)
