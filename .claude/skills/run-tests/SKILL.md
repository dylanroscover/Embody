---
name: run-tests
description: "Run Embody's test suite and write new tests (Embody development)"
disable-model-invocation: true
---

# Test Suite

Embody has 110+ test suites (2,500+ tests) under `dev/embody/unit_tests/` covering externalization, MCP tools, TDXN format, the Envoy server/bridge/jobs, install/upgrade paths, and infrastructure. Destructive and agent tiers are segregated behind their own entry points.

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
run_tests(background=True)           # all suites -- RECOMMENDED for full runs
get_job_status(job_id='job_...')     # poll; the finished record carries the summary
run_tests(suite_name='test_path_utils')   # small targeted runs may stay synchronous
```

For a FULL run always pass `background=True`: it returns a job id
immediately and results park restart-proof in `.embody/jobs/`. The
synchronous mode holds the HTTP call open for the whole run, and the Envoy
watchdog suites restart the very server it waits on -- the call is severed
("Server force-restarted / shutting down during test run") even though the
run finishes. (Pre-job-layer fallback, still valid: poll
`execute_python(code="result = op.unit_tests.GetResults()")` until the
totals stop moving.)

`RunTestsSync()` inside `execute_python` runs the whole suite INSIDE that
dispatch's undo block: the undo-guard tests fail (a block is already open)
and the entire run becomes one giant Ctrl+Z step.

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

Count suites with `ls dev/embody/unit_tests/test_*.py` -- the hand-maintained breakdown that used to sit here drifted 4x stale and was removed. Suite docstrings state their scope.

## After Running Tests

Always read log files at `dev/logs/` — the ring buffer only holds 200 entries. Grep for `ERROR` and `WARNING`.
