# Release smoke harness

Test tooling for Embody's own releases. Nothing here is a project to build on.

**`smoke_template.toe` clears its own folder.** With `smoke_bootstrap.py` beside it, opening it deletes everything in that folder except the template, the `smoke_run.json` sidecar and `.py` files, then installs the release `.tox`. That is what a fresh-install test needs and what a real project must never meet. To start a project, use [`starter/Embody-starter.toe`](../../starter/).

## Running it

    python dev/release_testing/smoke_run.py

The orchestrator stages a fresh per-run folder, launches TouchDesigner on it and reads the verdicts back. Do not open the template by hand; `docs/testing.md` has the full description.

## The baked script is a stub, and has to stay one

`/project1/execute1` in the template syncs to `smoke_bootstrap.py`: TouchDesigner loads that file over the DAT's saved text when the project opens. The text saved inside the `.toe` therefore only ever runs in a copy that was moved somewhere without the script, so it is an inert stub that prints one line.

It was not always. A unit-test fixture cut from this template carried the full cleanup, was used as a project base, and wiped that project's folder, `.git` included, on every open (issue #194).

Saving the template from TouchDesigner while the script is beside it bakes the full script back in. `test_embody_bootstrap::TestSmokeTemplate` fails when that happens. Put the stub back by expanding the template with `toeexpand`, replacing `project1/execute1.text` (framed by `embody_bootstrap.encode_text`) and collapsing it again; `starter/build_starter.py` shows the same steps.
