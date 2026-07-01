# Testing

Embody includes a comprehensive automated test suite with **75 test suites** and **1,751 test methods** covering core externalization, MCP tools, TDN format, the community/Collection safe-import path, the auto-save checkpoint engine, the Envoy server/bridge, and palette catalogs. Tests run inside TouchDesigner using a custom test runner with sandbox isolation.

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
# Returns: {'total': 1751, 'passed': 1751, 'failed': 0, 'errors': 0, 'skipped': 0, 'results': [...]}
```

### Via MCP

Use the `run_tests` Envoy tool:

```
run_tests()                              # Run all suites
run_tests(suite_name='test_path_utils')  # Run one suite
```

## Test Coverage

### Core Embody (25 suites, 408 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_duplicate_handling` | 43 | Duplicate / clone / replicant resolution |
| `test_custom_parameters` | 32 | Custom parameter behavior (Folder, Disable/Enable, Update, TDN controls, Logs, Envoy) |
| `test_tag_management` | 29 | Tagging operators for externalization |
| `test_rename_move_lifecycle` | 26 | Rename and move tracking |
| `test_crud_operators` | 21 | Create, read, update, delete operations |
| `test_git_status` | 20 | Git status / uncommitted-file detection |
| `test_param_tracker` | 20 | Parameter change tracking |
| `test_v6_hardening` | 20 | v6 community-paste + strip/restore hardening |
| `test_ancestor_rename` | 19 | Ancestor-rename detection and folder migration |
| `test_path_utils` | 18 | Path normalization and utilities |
| `test_autosave` | 18 | Auto-save checkpoint engine (skip_cleanup, idle-settle drain, crash recovery, gates) |
| `test_tag_lifecycle` | 17 | Tag application and removal |
| `test_delete_cleanup` | 16 | Deletion and file cleanup |
| `test_strategy_handlers` | 15 | TOX/TDN strategy switch, remove, DAT convert |
| `test_issue21_safe_cell` | 14 | Safe table-cell handling |
| `test_glsl_externalize` | 11 | GLSL shader auto-externalization |
| `test_layout_lint` | 10 | `execute_python` layout-warning linting |
| `test_logging` | 10 | Logging system and ring buffer |
| `test_externalization` | 9 | Externalization lifecycle |
| `test_file_management` | 8 | File I/O, path handling, tracked-file delete safety |
| `test_os_label` | 8 | OS label resolution (Win10/11 build thresholds, macOS) |
| `test_update_sync` | 7 | Sync between .toe and externalized files |
| `test_dialog_suppression` | 8 | File-cleanup dialog suppression during tests |
| `test_operator_queries` | 6 | Operator discovery and queries |
| `test_settings_persistence` | 3 | Settings serialization (byte-stable, sorted keys) |

### MCP Tools (15 suites, 193 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_mcp_externalization` | 22 | Embody integration via MCP (tag, save, status) |
| `test_mcp_operators` | 20 | Create, delete, copy, rename, query, find |
| `test_mcp_annotations` | 19 | Creating and managing annotations |
| `test_mcp_dat_content` | 19 | DAT text/table ops + surgical `edit_dat_content` + wipe guards |
| `test_mcp_diagnostics` | 16 | Error checking, class introspection, module help, log retrieval |
| `test_mcp_flags_position` | 16 | Operator flags, positioning, and `get_network_layout` |
| `test_mcp_project_performance` | 14 | Project-level FPS, memory, hotspots |
| `test_mcp_parameters` | 11 | Get/set parameters, modes, expressions |
| `test_mcp_top_capture` | 11 | TOP image capture (format, quality, resolution) |
| `test_mcp_tdn_tools` | 10 | `read_tdn`, `export_network`/`import_network` round-trip |
| `test_mcp_batch` | 9 | Batched multi-operation requests |
| `test_mcp_connections` | 8 | Wiring operators together |
| `test_mcp_code_execution` | 7 | Executing Python in TD |
| `test_mcp_extensions` | 6 | Extension creation and setup |
| `test_mcp_performance` | 5 | Per-operator performance monitoring |

### TDN Format (17 suites, 620 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_tdn_reconstruction` | 208 | Reconstruction round-trip fidelity |
| `test_tdn_file_io` | 92 | TDN file output, per-comp splitting, stale cleanup, tdn_ref / tox_ref pointers |
| `test_tdn_helpers` | 53 | TDN serialization utility functions |
| `test_tdn_export_import` | 44 | Network export/import + storage round-trip |
| `test_tdn_crash_safety` | 35 | Atomic writes, backup rotation, validation |
| `test_tdn_sequences` | 27 | Parameter / operator sequence round-trip |
| `test_tdn_diff_engine` | 25 | TDN structural diff engine |
| `test_tdn_palette_catalog` | 23 | Palette-clone detection and handling |
| `test_tdn_exclude` | 21 | `tdn_exclude` tag (app-managed subtree invisibility) |
| `test_tdn_mode` | 15 | Tdnmode gating (off / export / full) |
| `test_dat_restoration` | 14 | DAT restoration from disk on startup |
| `test_tdn_safety_guards` | 14 | At-risk storage / callback-DAT protection |
| `test_tdn_yaml` | 14 | TDN v2.0 YAML emitter / parser |
| `test_tdn_diff` | 11 | `diff_tdn` tool (live-vs-disk, project-wide) |
| `test_tdn_fingerprint` | 11 | Param-aware dirty detection (fingerprint) |
| `test_tdn_annotation_export` | 7 | Annotation-only `.tdn` export (annotateCOMP not double-captured) |
| `test_tdn_external_connections` | 6 | External wire capture/restore across strip |

### Community & Collection (6 suites, 123 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_clipboard_paste` | 42 | Clipboard auto-paste import flow |
| `test_collection_scanner` | 22 | Capability scanner verdicts (clean / flagged / blocked) |
| `test_specimen_publish` | 19 | Specimen publish hook |
| `test_collection_safe_import` | 18 | Safe-import `make_inert` disarming |
| `test_collection_pure` | 14 | Pure-value-expression preservation (live-if-clean) |
| `test_clipboard_watch` | 8 | Clipboard watcher poll + gating (incl. outbound-copy suppression) |

### Envoy Server & Bridge (9 suites, 358 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_envoy_bridge` | 156 | STDIO bridge: forwarding, reconciler, registry, meta-tools |
| `test_claude_config` | 83 | AI client config generation (Claude/Cursor/Copilot/Windsurf/Gemini) |
| `test_server_lifecycle` | 22 | Envoy MCP server start/stop |
| `test_envoy_watchdog` | 21 | Envoy liveness watchdog (revive on dropped socket / save) |
| `test_envoy_thread_comm` | 20 | Worker/main thread queues and throttling |
| `test_envoy_setup_environment` | 18 | MCP import verification (pydantic_core safety) |
| `test_envoy_registry` | 17 | Instance registry and PID liveness |
| `test_launch_aiclient` | 17 | Launch AI Client launcher (launch table, CLI resolution, `.command` generation, env sanitization) |
| `test_envoy_lifecycle_hardening` | 4 | Save/reinit lifecycle hardening |

### Catalog & Release (3 suites, 49 tests)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_smoke_release` | 33 | Release smoke checks (extensions loaded, Envoy state) |
| `test_catalog_bootstrap_palette` | 10 | Bootstrap palette table parsing + build coverage |
| `test_catalog_palette_scan` | 6 | Palette scan time-state snapshot/restore |

## Execution Modes

| Mode | Method | Behavior |
|------|--------|----------|
| Per-test deferred | `RunTests()` | One test per frame. Best for heavy suites. Non-blocking. **Default.** |
| Per-suite deferred | `RunTestsDeferred()` | One suite per frame. Keeps TD responsive. |
| Synchronous | `RunTestsSync()` | All tests in one frame. Blocks TD. Use for MCP. |

## Test Framework Features

- **Sandbox isolation**: Each suite gets a fresh `baseCOMP` for test fixtures
- **20+ assertion methods**: `assertEqual`, `assertTrue`, `assertIn`, `assertIsInstance`, etc.
- **Lifecycle hooks**: `setUp`/`tearDown` per test, `setUpSuite`/`tearDownSuite` per suite
- **Skip support**: `self.skip(reason)` to conditionally skip tests
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
