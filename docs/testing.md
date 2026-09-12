# Testing

Embody includes a comprehensive automated test suite with **152 test suites** and **4,959 test methods** (the three agent-tier suites run only on request) covering core externalization, MCP tools, TDXN format, the community/Collection safe-import path, the auto-save checkpoint engine, Envoy server/session coordination, launch/config generation, install/uninstall paths, and palette catalogs. Tests run inside TouchDesigner using a custom test runner with sandbox isolation; the pure-Python suites also run under pytest, and a few run only there.

## Running Tests

### Local pytest tier

Most suites run only inside TouchDesigner, but the TD-import-free ones
(listed in `pytest.ini`: the Convoy host app plus the pure-Python Embody
suites) also run headless under pytest -- the same set CI runs on the
windows + macOS matrix. Suites written as plain pytest functions run only
there (see the pytest-only table under [Test Coverage](#test-coverage)).

Use a **dedicated venv built from TouchDesigner's own interpreter**. The
version has to match what ships (`conftest.py` refuses any other), but the
venv must not inherit TD's bundled packages -- whatever is importable in
the running interpreter can change a test's verdict:

```bash
"<TD>/bin/python.exe" -m venv dev/.venv-tests
dev/.venv-tests/Scripts/python.exe -m pip install -r dev/embody/unit_tests/requirements-test.txt
dev/.venv-tests/Scripts/python.exe -m pytest
```

`requirements-test.txt` is the single source of truth for that dependency
set -- CI installs from the same file. Do **not** install test packages
into `dev/.venv`: that is Embody's live Envoy runtime venv, and test-only
packages there can mask a missing dependency in the shipped path.

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
# {'total': 3684, 'passed': 3684, 'failed': 0, 'errors': 0, 'skipped': 0, 'results': [...]}
```

### Via MCP

Use the `run_tests` Envoy tool:

```
run_tests()                              # Run all suites
run_tests(suite_name='test_path_utils')  # Run one suite
```

## Test Coverage

The tables below cover 149 suites. The remaining three are the
AI-client connectivity tier, listed separately under
[Agent Tier](#agent-tier-ai-client-connectivity-tests).

### Core Embody (42 suites, 783 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_startup_progress` | 111 | The status readout's signals: the state each subsystem's status string maps to (including the idle states that used to read as work in progress), the age an auto-save stamp becomes, the elapsed clock a wedged step must show, and the rows `viz_status` renders -- including the predicate that decides whether the panel may re-arm a tick at all, and the mutation guard over the removed publish machinery. Pure module, so it also runs under plain pytest on the CI matrix |
| `test_duplicate_handling` | 43 | Duplicate / clone / replicant resolution |
| `test_custom_parameters` | 32 | Custom parameter behavior (Folder, Disable/Enable, Update, TDXN controls, Logs, Envoy) |
| `test_tag_management` | 29 | Tagging operators for externalization |
| `test_rename_move_lifecycle` | 28 | Rename and move tracking |
| `test_crud_operators` | 21 | Create, read, update, delete operations |
| `test_git_status` | 23 | Git status / uncommitted-file detection |
| `test_param_tracker` | 20 | Parameter change tracking |
| `test_v6_hardening` | 20 | v6 community-paste + strip/restore hardening |
| `test_ancestor_rename` | 19 | Ancestor-rename detection and folder migration |
| `test_path_utils` | 20 | Path normalization and utilities |
| `test_autosave` | 35 | Auto-save checkpoint engine (skip_cleanup, idle-settle drain, crash recovery, gates) |
| `test_tag_lifecycle` | 18 | Tag application and removal |
| `test_delete_cleanup` | 17 | Deletion and file cleanup |
| `test_auto_externalize` | 16 | Auto-externalization flow and eligible operator handling |
| `test_shortcuts` | 48 | Editable keyboard shortcuts: combo normalization, dispatch, TD reserved-list parsing, duplicate blocking, recorder state machine, parexec handlers, persistence whitelist |
| `test_strategy_handlers` | 22 | TOX/TDXN strategy switch, remove, DAT convert |
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
| `test_component_presentation` | 4 | Shipped-component presentation invariants |
| `test_verify_upgrade` | 5 | Upgrade-path validation (the removed Skip/Re-scan dialog) |
| `test_annotate_continuity` | 7 | Continuity sweep vs utility `annotateCOMP` rows: bare `op()` cannot resolve a utility annotate, so a legacy row at one read as a vanished operator and had its row + `.tdxn` deleted on every save |
| `test_project_saved_gate` | 11 | The one "is this project saved?" authority (`_projectSavedOnDisk`), keyed on the project name rather than a file at `project.folder` |
| `test_promoted_surface` | 14 | Promoted-surface census: every capitalized extension member is deliberate public API, and every documented `op.Embody` method still resolves (issue #94) |
| `test_pardef` | 15 | Get-or-create custom pages and parameters: an extension reinit never replaces a parameter the user set (issue #94) |
| `test_save_path_contracts` | 20 | Save-path write contracts: `_updateRowCells` is the sole writer of the build, timestamp, dirty and position cells |
| `test_no_console_window` | 7 | No stray console windows when Embody spawns console programs (git, uv, pip, schtasks) on Windows, plus save-path de-duplication |
| `test_thread_safety_guards` | 7 | Runtime guards from the 2026-08-17 Derivative threading advisory; TD-import-free, so they also run on the pytest matrix |

### MCP Tools (23 suites, 426 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_envoy_tool_guards` | 66 | Envoy tool safety guards (undo blocks, parameter guards/search, rollback, docs, sample grid, host-destroy refusals) |
| `test_recovery_hints` | 22 | Recovery-hint match table vs. real error strings + the additive `_attachRecoveryHints` decorator |
| `test_mcp_externalization` | 26 | Embody integration via MCP (tag, save, status) |
| `test_mcp_operators` | 20 | Create, delete, copy, rename, query, find |
| `test_mcp_annotations` | 31 | Creating and managing annotations |
| `test_mcp_dat_content` | 19 | DAT text/table ops + surgical `edit_dat_content` + wipe guards |
| `test_mcp_diagnostics` | 21 | Error checking across all three surfaces (cook, script tracebacks, shader), class introspection, module help, log retrieval |
| `test_mcp_flags_position` | 16 | Operator flags, positioning, and `get_network_layout` |
| `test_mcp_project_performance` | 14 | Project-level FPS, memory, hotspots |
| `test_mcp_parameters` | 11 | Get/set parameters, modes, expressions |
| `test_mcp_top_capture` | 15 | TOP image capture (format, quality, resolution) + black/flat/transparent quality verdict |
| `test_mcp_tdxn_tools` | 11 | `read_tdn`, `export_network`/`import_network` round-trip |
| `test_mcp_batch` | 9 | Batched multi-operation requests |
| `test_mcp_connections` | 10 | Wiring operators together |
| `test_mcp_code_execution` | 7 | Executing Python in TD |
| `test_mcp_extensions` | 6 | Extension creation and setup |
| `test_mcp_performance` | 5 | Per-operator performance monitoring |
| `test_tool_permissions` | 15 | Tool-permissions posture writer (EnvoyExt) |
| `test_envoy_viz_gates` | 47 | Issue-57 viz activation gates in `envoy_viz` |
| `test_envoy_tool_schema` | 8 | Tool-wrapper/handler signature conformance across every registered MCP tool (forwarded-but-unaccepted, required-but-unforwarded, advertised-but-ignored, duplicate dispatch). Static AST analysis -- invokes no tools |
| `test_data_readers` | 22 | `get_chop_data` / `get_pop_data`: reduced reads (per-channel stats, channel globs and caps, relational diffs), never a raw dump |
| `test_mcp_capture_op` | 9 | `capture_op`: any operator through a transient OP Viewer TOP, waiting out the empty frames a freshly aimed viewer returns |
| `test_shader_diagnostics` | 16 | GLSL compile failures surfaced on `get_op_errors` (TD only warns; the details live in the Info DAT) |

### TDXN Format (23 suites, 878 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_tdxn_reconstruction` | 236 | Reconstruction round-trip fidelity + script-error reporting in the rebuild report |
| `test_tdxn_file_io` | 100 | TDXN file output, per-comp splitting, stale cleanup, tdn_ref / tox_ref pointers |
| `test_tdxn_helpers` | 120 | TDXN serialization utility functions, locked-content source classification and Switch to TOX |
| `test_tdxn_export_import` | 65 | Network export/import + storage round-trip |
| `test_tdxn_crash_safety` | 48 | Atomic writes, backup rotation, validation |
| `test_tdxn_sequences` | 31 | Parameter / operator sequence round-trip |
| `test_tdxn_diff_engine` | 25 | TDXN structural diff engine |
| `test_tdxn_palette_catalog` | 34 | Palette-clone detection and handling |
| `test_tdxn_exclude` | 27 | `tdxn_exclude` tag (app-managed subtree invisibility) |
| `test_tdxn_stability_hardening` | 21 | Import validation, DAT editability capture, flag defaults, stale cleanup, orphan shell recovery |
| `test_tdxn_mode` | 16 | Tdxnmode gating (off / export / full) |
| `test_dat_restoration` | 20 | DAT restoration from disk on startup |
| `test_tdxn_safety_guards` | 35 | Save-time TDXN content report: storage severity per mode, the DAT tripwire, deferred filing, managed-type filters |
| `test_tdxn_yaml` | 14 | TDXN v2.0 YAML emitter / parser |
| `test_tdxn_diff` | 11 | `diff_tdn` tool (live-vs-disk, project-wide) |
| `test_tdxn_fingerprint` | 20 | Param-aware dirty detection (fingerprint) |
| `test_tdxn_annotation_export` | 10 | Annotation-only `.tdxn` export (annotateCOMP not double-captured) |
| `test_tdxn_external_connections` | 6 | External wire capture/restore across strip |
| `test_tdxn_export_progress` | 6 | Chunked TDXN export progress dialog + cancellation (and that it leaves no transient state behind) |
| `test_tdxn_roundtrip_invariant` | 6 | Writer/reader contract: the exporter must never emit a document its own importer rejects (no empty sequence lists, no sequence silently dropped from an uncooked POP) |
| `test_tdxn_migration` | 18 | The v6.1 migration of existing `.tdn` files (`migrateToTDXN`), the one path that moves user files, on a full three-level project |
| `test_tdxn_par_key_migration` | 7 | `config.json` key migration for the renamed TDXN parameters, so a stored setting survives the rename |
| `test_tdxn_schema` | 2 | Every committed TDXN document validates against the shipped `docs/tdxn.schema.yaml` (contract C7) |

### Community & Collection (7 suites, 129 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_clipboard_paste` | 42 | Clipboard auto-paste import flow |
| `test_collection_scanner` | 22 | Capability scanner verdicts (clean / flagged / blocked) |
| `test_specimen_publish` | 19 | Specimen publish hook |
| `test_collection_safe_import` | 18 | Safe-import `make_inert` disarming |
| `test_collection_pure` | 14 | Pure-value-expression preservation (live-if-clean) |
| `test_clipboard_watch` | 9 | Clipboard watcher poll + gating (incl. outbound-copy suppression) |
| `test_scanner_parity` | 5 | Capability-scanner parity between `scanner.py` and `scanner-ts` over the shared fixture corpus (contract C8) |

### Envoy Server & Bridge (19 suites, 1181 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_envoy_bridge` | 490 | STDIO bridge: forwarding, reconciler, registry, meta-tools, and `crash_detected` lifecycle TRANSITIONS (reconnect clears, pid re-resolution honours the session pin, a foreign instance on the port never clears a real crash) |
| `test_claude_config` | 140 | AI client config generation and the restore-on-open probes (Claude Code/OpenCode/Codex/Gemini/VS Code/Cursor/Windsurf/GitHub Copilot) |
| `test_envoy_sessions` | 57 | Multi-session awareness, scope claims, peer advisories, destructive-operation gates |
| `test_server_lifecycle` | 24 | Envoy MCP server start/stop |
| `test_envoy_watchdog` | 80 | Envoy liveness watchdog (revive on dropped socket / save) |
| `test_version_sync` | 19 | Version badge / minimum-build statements stay in lock-step with `par.Version` and `app.build` |
| `test_envoy_thread_comm` | 20 | Worker/main thread queues and throttling |
| `test_launch_aiclient` | 54 | Launch AI Client launcher (launch table with per-OS install specs, CLI resolution, `.command`/`.bat` generation, missing-CLI install guards and their shell escaping, failure dialogs, env sanitization) |
| `test_envoy_setup_environment` | 47 | MCP import verification (pydantic_core safety) |
| `test_envoy_registry` | 18 | Instance registry and PID liveness |
| `test_opencode_config` | 17 | OpenCode client config writer (`envoy_setup.write_opencode_config`) |
| `test_ai_clients` | 56 | The `ai_clients` registry (row schema, launch-alias resolution, uninstall footprint) and the per-client MCP config writers -- Cursor/VS Code/Copilot/Gemini/Codex/Antigravity dialects, conservative merging, JSONC safety, and the launch-target vs Configure-For split |
| `test_agent_runner` | 15 | AGENT-tier runner machinery in TestRunnerExt (the runner itself, no LLM) |
| `test_envoy_lifecycle_hardening` | 8 | Save/reinit lifecycle hardening, and the cached-repo-root guard (a non-absolute root must never reach the job layer, task ledger, or docs catalog) |
| `test_envoy_agent_ux` | 60 | Agent-ergonomics surface: `get_guidance`, the write-effect footer, `get_focus` |
| `test_job_layer` | 29 | Background-job layer backing `get_job_status` / `save_project` / `run_tests background=True` -- the operations that outlive the 30 s MCP timeout |
| `test_task_ledger` | 15 | Shared task ledger (`.embody/tasks.json`): work-STATE across AI sessions, including `done_uncommitted` |
| `test_envoy_bridge_dialogs` | 22 | `list_dialogs` / `dismiss_dialog` bridge meta-tools: finding TD-drawn dialogs by window ownership and closing them |
| `test_envoy_bridge_instance_arg` | 10 | Per-call `instance` routing in the STDIO bridge: one call reaches a named instance without moving the session pin |

### Convoy (9 suites, 850 tests)

The LAN work relay: the node-side reconciler, the host-app client and installer, and the panel contracts. The host app itself is stdlib-only by design, so most of this tier also runs under plain pytest on the windows+macos CI matrix -- see `pytest.ini`.

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_convoy_install` | 308 | Host-app installer: Scheduled Task XML, LaunchAgent plist, the launcher, and the per-user daemon venv preference |
| `test_convoy_client` | 180 | Host-app client, and its PARITY with the daemon-side probe the two copies must not drift from |
| `test_convoy_host_install` | 108 | ConvoyExt's host-app orchestration (install plan) |
| `test_convoy_ext` | 81 | ConvoyExt, the node-side Convoy reconciler |
| `test_convoy_parameter_contract` | 47 | Off-TD contract for Convoy's user-facing parameter scaffold |
| `test_convoy_host_ladder` | 78 | The runtime ladder that decides WHICH interpreter the daemon runs under, the register-success revive, the spawn-blocked verdict (supervisor-launched TD), and the startup construction kick |
| `test_convoy_realm_recovery` | 23 | The TD-side realm-conflict recovery: `Resolve Realm Conflict` / `Join Other Realm` worker bodies and the rejoin rebind |
| `test_wizard_convoy_contract` | 20 | Plain-Python contracts for Convoy's setup-wizard routing |
| `test_convoy_forget_ux` | 5 | The Forget Offline Nodes daemon -> panel contract |

### Install, Uninstall & Release (21 suites, 557 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_smoke_release` | 36 | Release smoke checks (extensions loaded, Envoy state, Uninstall pulse/handler shipped) |
| `test_install_manifest` | 12 | Generated install manifest and packaged config coverage |
| `test_uninstall_execute` | 11 | Uninstall execution path and cleanup safety |
| `test_uninstall_preview` | 13 | Uninstall preview plan and protected-file handling |
| `test_uninstall_handler` | 5 | Uninstall pulse confirm gate (cancel/suppress/confirm/review) |
| `test_catalog_bootstrap_palette` | 10 | Bootstrap palette table parsing + build coverage |
| `test_catalog_palette_scan` | 39 | Palette scan time-state snapshot/restore |
| `test_template_sync` | 6 | Template map, disk, release-table, and orphan allowlist sync |
| `test_release_hooks` | 59 | `ExportPortableTox` release hooks (issue #74) |
| `test_updater` | 93 | UpdaterExt self-update logic (no network, no swap) |
| `test_config_migration` | 41 | Repo-config writers across a VERSION BUMP -- the migration axis a single-run test cannot see (duplicate managed headers, block consolidation that never swallows user content, `.gitattributes` backfill, the order-dependent `.embody/*` / `!.embody/project.json` pair, and the `git check-ignore` respect for a repo that deliberately ignores `project.json`), and retiring the old `.tdxn` git diff driver |
| `test_embody_pyenv` | 63 | The shared project Python environment -- constraints, declared extras stewardship, DLL-path parity, tdPyEnvManager detection |
| `test_pyenv_context` | 36 | TD pre-cook venv context authoring -- render/classify/status/refresh, the foreign-context hands-off contract, gitignore + manifest footprint helpers |
| `test_wizard_externalize` | 20 | The setup wizard's externalize step, its recovery point, and the already-externalized detection |
| `test_dialog_wrap` | 5 | Every dialog's prose wraps to readable lines (`ui.messageBox` sizes itself to its longest line) |
| `test_embody_bootstrap` | 18 | `embody_bootstrap.py`: offline install of Embody into a `.toe` (toc parsing, graft planning, provisioning DAT, manifest verification) |
| `test_release_artifact_contract` | 2 | What the shipped `.tox` must not carry: expands the newest release with `toeexpand` and checks its storage and table link |
| `test_release_toe` | 69 | `ExportReleaseToe` planners, off-TD: one test per refusal, the save path, and the scrub plan |
| `test_release_toe_live` | 4 | The read side of `ExportReleaseToe`, in TD (the export itself destroys Embody and quits, so it never runs under the runner) |
| `test_catalog_default_patch` | 6 | CatalogManager cross-build default repair (`_patchComp`) for parameters whose default TD changed between builds |
| `test_pyenv_context_live` | 9 | The live gating state machine around the TD pre-cook venv context (`_ensurePyEnvContext`) |

### pytest-only suites (5 suites, 149 tests)

Plain pytest functions, which the in-TD runner does not collect: these run only under pytest, locally and on the CI matrix.

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_host_destroy_lint` | 66 | The `execute_python` host-destroy lint (`envoy_guard.py`) and the structured guard in EnvoyExt (issue #110) |
| `test_save_warnings` | 19 | The save-time report: which lines a `save_project` job record lists, and when storage earns a WARNING (issue #109) |
| `test_worker_run_lint` | 26 | Static lint for the global `run()` reached from a worker thread (2026-08-17 Derivative advisory) |
| `test_release_preflight` | 18 | The `/release` GitHub preflight: open alerts, Dependabot PRs, red CI and unmerged hotfixes block a release |
| `test_pytest_kill_fence` | 20 | The repo-root `conftest.py` fence: a run may terminate only processes it spawned, and gets its own temp dir |

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
