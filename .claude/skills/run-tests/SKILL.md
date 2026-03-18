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

**Via MCP:**
```python
execute_python(code="op.unit_tests.RunTestsSync(); result = op.unit_tests.GetResults()")
```

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
