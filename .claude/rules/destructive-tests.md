# Destructive Whole-Project Tests

Some test suites exercise Embody operations that mutate the **entire live
project**, not an isolated sandbox: `Disable()`, `ExternalizeProject()` /
`_externalize_project_silent()`, and `Reset()` all iterate `ext.root` and touch
every tracked operator project-wide. `Disable()` in particular `unlink()`s every
tracked file and clears the externalizations table; a follow-on externalize
re-tags and re-writes every COMP.

These MUST be tested (the Disable/Enable lifecycle can regress), but running them
as part of a normal suite is catastrophic. This rule is a dev-only convention for
the Embody test harness -- it has no shipped template counterpart.

## The incident this prevents (2026-07-01)

A full `RunTests()` run included `test_custom_parameters`, whose
`Disable(removeTags=True)` + `_externalize_project_silent(use_tdn=False)` ran
against the live project. It deleted **18 crown-jewel specimen `.tdn` files**
project-wide and re-tagged every COMP to TOX. The root cause was NOT the
continuity sweep -- it was a destructive suite running against `ext.root` during
a normal run, amplified by `Filecleanup` being stuck at `delete` (silent
unlinks). Recovery required rebuilding the specimen `.tdn` from the live COMPs.

## The rules

1. **Tag destructive suites.** Any suite that calls `Disable`,
   `ExternalizeProject` / `_externalize_project_silent`, or `Reset` on `ext.root`
   (the live project) sets a class attribute `DESTRUCTIVE = True`, with a comment
   explaining what it mutates.

2. **Never run them in a normal run.** `_discoverTestSuites` EXCLUDES
   `DESTRUCTIVE` suites from `RunTests` / `RunTestsSync` / `RunTestsDeferred*`.
   A plain full run must never be able to touch the live project. Do not remove
   this guard.

3. **Only via the save-gated batch.** They run ONLY through
   `RunDestructiveTests(confirm_saved=True)`, which:
   - is opt-in (`confirm_saved` must be True),
   - refuses if `project.dirty` (a saved `.toe` must exist as a recovery point),
   - runs the destructive suites in isolation,
   - logs a reminder that the live network is now mutated and the saved `.toe`
     must be reopened to restore it.

4. **ALWAYS save before running them, and reopen after.** `project.save()` first
   (the recovery point), then `RunDestructiveTests(confirm_saved=True)`, then
   reopen the saved `.toe` to restore the live session. Never run destructive
   tests against unsaved crown-jewel work.

5. **Never leave `Filecleanup` stuck at `delete`.** The test runner forces it to
   `delete` during runs and restores it in `_restoreFileCleanupDialog` with a
   re-entrancy guard so an interrupted/timed-out batch cannot leave it stuck --
   a stuck `delete` turns any file operation into a silent unlink (the amplifier
   that turned a strategy flip into 18 deleted files).

## For the AI agent

- Do NOT run the full test suite against a live project that holds unsaved,
  uncommitted work (especially specimen `.tdn`). Save first.
- A normal `RunTests()` is now safe (destructive suites are excluded). To test
  the Disable/externalize lifecycle, save, then call
  `RunDestructiveTests(confirm_saved=True)`, then reopen the saved `.toe`.
- This complements the general "save before running tests" discipline: the suite
  is a stress test, and destructive suites can mutate the live project.
