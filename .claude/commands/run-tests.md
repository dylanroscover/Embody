Load the `/run-tests` skill, then run the full Embody test suite via MCP:

1. Execute `op.unit_tests.RunTestsSync()` and `op.unit_tests.GetResults()` via `execute_python`
2. Report the results summary (pass/fail counts per suite)
3. If any failures, read `dev/logs/` for the full error context
4. If a specific suite or test name is provided as $ARGUMENTS, run only that: `RunTestsSync(suite_name='...')` or `RunTestsSync(suite_name='...', test_name='...')`
