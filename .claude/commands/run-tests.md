Load the `/run-tests` skill, then run the full Embody test suite via MCP:

1. If the project holds unsaved work, `project.save()` first -- the saved `.toe` is the recovery point
2. Run via the `run_tests` MCP tool (deferred runner). Do NOT call `RunTestsSync()` inside `execute_python`: the dispatch wraps that call in an undo block, which makes the undo-guard tests fail (a block is already open) and turns the whole run into one giant Ctrl+Z step. If the MCP call dies mid-run with a server-restart error (the Envoy watchdog suites restart the server it waits on), the run continues -- poll `op.unit_tests.GetResults()` via `execute_python` until the totals stop moving
3. Report the results summary (pass/fail counts per suite)
4. Always read `dev/logs/` afterward -- pass or fail -- and grep for `ERROR` and `WARNING` (the ring buffer only holds 200 entries)
5. If a specific suite or test name is provided as $ARGUMENTS, run only that: `RunTestsSync(suite_name='...')` or `RunTestsSync(suite_name='...', test_name='...')`
