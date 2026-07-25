Load the `/run-tests` skill, then run the full Embody test suite via MCP:

1. If the project holds unsaved work, `project.save()` first -- the saved `.toe` is the recovery point
2. Execute `op.unit_tests.RunTestsSync()` and `op.unit_tests.GetResults()` via `execute_python`
3. Report the results summary (pass/fail counts per suite)
4. Always read `dev/logs/` afterward -- pass or fail -- and grep for `ERROR` and `WARNING` (the ring buffer only holds 200 entries)
5. If a specific suite or test name is provided as $ARGUMENTS, run only that: `RunTestsSync(suite_name='...')` or `RunTestsSync(suite_name='...', test_name='...')`
