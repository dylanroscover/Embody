---
name: run-tests
description: "Run Embody's test suite and write new tests (Embody development)"
disable-model-invocation: true
---

# Test Suite

Embody has 30 test suites covering core externalization, MCP tools, TDN format, and infrastructure.

## Running Tests

**From TouchDesigner:**
```python
op.unit_tests.RunTests()                          # All tests, one per frame
op.unit_tests.RunTests(suite_name='test_path_utils')  # Specific suite
op.unit_tests.RunTests(suite_name='test_path_utils', test_name='test_normalizePath_backslashes_converted')
op.unit_tests.RunTestsSync()                      # Synchronous (blocks TD)
results = op.unit_tests.GetResults()              # Get results dict
```

**Via MCP -- use the `run_tests` tool, NOT RunTestsSync inside execute_python:**
```python
run_tests()                          # all suites (deferred runner)
run_tests(suite_name='test_path_utils')
```

`RunTestsSync()` inside `execute_python` runs the whole suite INSIDE that
dispatch's undo block: the undo-guard tests fail (a block is already open)
and the entire run becomes one giant Ctrl+Z step. If `run_tests` dies
mid-run with a server-restart error (the Envoy watchdog suites restart the
very server it waits on), the run continues -- poll
`execute_python(code="result = op.unit_tests.GetResults()")` until the
totals stop moving.

## Writing New Tests

Create a test file in `dev/embody/unit_tests/`:

```python
"""Test suite: description of what this tests."""

class TestMyFeature(EmbodyTestCase):
    def test_something(self):
        """Test description."""
        op = self.sandbox.create(baseCOMP, 'test_op')
        result = self.embody_ext.someMethod(op)
        self.assertEqual(result, expected_value)
        self.assertTrue(op.valid)
        self.assertIn('foo', result)

    def setUp(self):
        pass

    def tearDown(self):
        super().tearDown()  # Cleans up sandbox
```

**Key objects:** `self.sandbox` (temp baseCOMP), `self.embody` (op.Embody), `self.embody_ext` (op.Embody.ext.Embody), `self.runner` (TestRunnerExt). All TD globals available.

## Test Coverage

**Core (14):** externalization, CRUD, file management, tags, rename/move, delete, duplicates, sync, paths, params, queries, logging, custom parameters
**MCP (11):** operators, parameters, DAT content, connections, annotations, extensions, diagnostics, flags/position, code execution, externalization, performance
**TDN (4):** export/import, helpers, reconstruction, file I/O
**Infrastructure (1):** server lifecycle

## After Running Tests

Always read log files at `dev/logs/` — the ring buffer only holds 200 entries. Grep for `ERROR` and `WARNING`.
