# Testing

Embody includes a comprehensive automated test suite with **30 test suites** and **587 test methods** covering core externalization, MCP tools, TDN format, and server lifecycle. Tests run inside TouchDesigner using a custom test runner with sandbox isolation.

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
# Returns: {'total': 587, 'passed': 587, 'failed': 0, 'errors': 0, 'skipped': 0, 'results': [...]}
```

### Via MCP

Use the `run_tests` Envoy tool:

```
run_tests()                              # Run all suites
run_tests(suite_name='test_path_utils')  # Run one suite
```

## Test Coverage

### Core Embody (14 suites)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_externalization` | 9 | Externalization lifecycle |
| `test_crud_operators` | 21 | Create, read, update, delete operations |
| `test_file_management` | 7 | File I/O, path handling, cleanup |
| `test_tag_management` | 19 | Tagging operators for externalization |
| `test_tag_lifecycle` | 17 | Tag application and removal |
| `test_rename_move_lifecycle` | 25 | Rename and move tracking |
| `test_delete_cleanup` | 16 | Deletion and file cleanup |
| `test_duplicate_handling` | 4 | Duplicate operator handling |
| `test_update_sync` | 7 | Sync between .toe and externalized files |
| `test_path_utils` | 18 | Path normalization and utilities |
| `test_param_tracker` | 13 | Parameter change tracking |
| `test_operator_queries` | 6 | Operator discovery and queries |
| `test_logging` | 10 | Logging system |
| `test_custom_parameters` | 30 | Custom parameter behavior (Folder, Disable/Enable, Update, TDN controls, Logs, Envoy) |

### MCP Tools (11 suites)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_mcp_operators` | 20 | Create, delete, copy, rename, query, find |
| `test_mcp_parameters` | 11 | Get/set parameters, modes, expressions |
| `test_mcp_dat_content` | 9 | DAT text and table operations |
| `test_mcp_connections` | 8 | Wiring operators together |
| `test_mcp_annotations` | 19 | Creating and managing annotations |
| `test_mcp_extensions` | 6 | Extension creation and setup |
| `test_mcp_diagnostics` | 12 | Error checking, performance, info |
| `test_mcp_flags_position` | 12 | Operator flags and positioning |
| `test_mcp_code_execution` | 7 | Executing Python in TD |
| `test_mcp_externalization` | 18 | Embody integration via MCP |
| `test_mcp_performance` | 5 | Performance monitoring |

### TDN Format (4 suites)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_tdn_export_import` | 26 | Network export/import |
| `test_tdn_helpers` | 25 | TDN utility functions |
| `test_tdn_reconstruction` | 124 | Reconstruction round-trip fidelity |
| `test_tdn_file_io` | 66 | TDN file output, per-comp splitting, stale cleanup |

### Infrastructure (1 suite)

| Suite | Tests | Coverage |
|-------|-------|----------|
| `test_server_lifecycle` | 17 | Envoy MCP server start/stop |

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
