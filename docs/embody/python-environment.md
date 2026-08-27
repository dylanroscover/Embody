# Python Environment

Embody maintains a Python virtual environment for every project at
`.venv/` next to your `.toe` -- built with [uv](https://docs.astral.sh/uv/)
against TouchDesigner's own embedded interpreter, so every wheel matches
TD's Python version and architecture. Envoy's MCP stack runs from it, the
`.tdxn` git diff driver runs from it, and **your own packages can live in
it too**.

The environment is shared three ways:

1. **Imports inside TouchDesigner.** The venv's `site-packages` is wired
   into `sys.path` *before any component cooks*: Embody writes a small
   `TDPyEnvManagerContext.yaml` beside your `.toe` (see
   [Pre-cook linking](#pre-cook-linking)) that TouchDesigner's own
   startup helper applies pre-cook, and Embody's extension wires the
   paths again at its own init as the fallback. Any DAT, extension, or
   Script operator in the project can `import` an installed package --
   including at module level in an extension that constructs before
   Embody does. (Deferred imports remain a sensible belt-and-suspenders
   for the very first open of a fresh clone, before the venv exists.)
2. **External scripts.** `op.Embody.VenvPython` returns the venv's
   interpreter path -- run helper scripts, build steps, or cron jobs
   against the exact environment the project uses.
3. **Other machines.** Packages you add are *declared* in the committed
   `.embody/project.json`, so the declaration travels with the repo --
   and each machine approves it once before anything installs (see
   [Security model](#security-model)).

## Installing packages

```python
op.Embody.InstallPackages('websockets')
op.Embody.InstallPackages(['pandas>=2.1', 'python-osc'])
```

`InstallPackages` returns immediately and installs in the background --
TouchDesigner never blocks. Progress and results land in the Embody log
(the textport, and the log folder beside your project -- `logs/` by
default). Once the log reports the install finished,
the package is importable right away; no restart needed for *new*
packages.

What happens under the hood:

- The request is validated (see [Refusals](#what-gets-refused) below).
  Only plain, name-based requirements are accepted -- no URLs, VCS
  references, or file paths.
- Accepted specs are recorded in `.embody/project.json` under
  `python.extras` -- **commit this file** so the declaration travels
  with the project -- and approved for this machine in the local (not
  committed) `.embody/local.json`.
- The install is **wheels-only** (`--no-build`): source distributions
  execute arbitrary code at build time, so a package without a
  pre-built wheel fails with a clear message instead of silently
  building.
- The install runs constrained by a snapshot of Embody's own dependency
  tree, so a package cannot move the MCP server's stack out from under a
  running session. (On a venv that predates this feature the snapshot
  doesn't exist yet; until the next core update the constraint falls
  back to Embody's direct pins, which protect the core packages
  themselves but not their sub-dependencies.)
- If one package in a batch fails to resolve, the others still install.
  Resolver conflicts are remembered and not retried until you request
  that package again (or change its version spec); transient failures
  (a locked file, a network blip) retry automatically on later starts,
  up to three attempts -- after that a warning names the package, and
  re-requesting it resets the counter.

**Removing a package:** delete its entry from `python.extras` in
`.embody/project.json`. The package stays installed until the next venv
rebuild (a TouchDesigner Python upgrade, an architecture migration) --
to remove it immediately:
`uv pip uninstall <name> --python "<VenvPython>"`. (The venv carries no
`pip` of its own -- uv-built environments are managed through uv.)

## Security model

Installing a package is running someone's code. Two rules keep that
honest:

- **A pulled declaration never auto-installs.** `python.extras` travels
  in git, so a teammate's commit (or a PR you merged) can add entries.
  Those show up as a **warning** listing the pending packages; nothing
  installs until someone at this machine runs
  `op.Embody.ApplyDeclaredExtras()`. Your own `InstallPackages` calls
  are approved implicitly -- consent is per-machine, stored in the
  uncommitted `.embody/local.json`.
- **Review `python.extras` diffs like code**, because that is what they
  are: an approved entry installs a third-party package on every
  machine that applies it.

AI agents get the same story in the generated project instructions:
ask the user before calling `InstallPackages`.

## What gets refused

Three categories are refused, each with a logged reason:

- **Non-name-based requirements**: URLs, `git+...` references,
  `pkg @ https://...` direct references, and file paths. These can carry
  any package under any name, so none of the safety checks would mean
  anything for them.
- **Embody's core stack** (`mcp`, `attrs`, `pyyaml`, `cryptography`,
  `pywin32`, and the packaging tools `pip`/`uv`/`setuptools`/`wheel`):
  their versions are managed by Embody releases.
- **Packages TouchDesigner bundles** (`numpy`, `opencv-python`, and
  whatever else your TD build ships): a different version loaded from
  the venv can crash TD operators that link against the bundled build.
  `allow_shadow=True` overrides per call and records the opt-in in the
  declaration (`python.extras_allow_shadow`) so it survives rebuilds.
  Note the opt-in list is committed and travels with the project; the
  protection against a *pulled* shadow entry is the same per-machine
  approval gate as for any other pulled declaration.

## Upgrades, rebuilds, and restarts

- A TouchDesigner upgrade that changes the embedded Python (or moving a
  project between Intel and Apple Silicon Macs) rebuilds the venv from
  scratch -- binary wheels are tied to the interpreter that built them.
  Approved declared packages are re-installed automatically afterward.
- If an install *changes* a package that is already imported in the
  running session -- including a dependency you never named -- Embody
  logs a restart notice instead of hot-swapping it: reloading a live
  native extension can crash TD. Save and restart to finish that
  change.

## Running external scripts

```python
python = op.Embody.VenvPython   # '' until the venv exists
site   = op.Embody.VenvSitePackages
```

```bash
"C:/path/to/project/.venv/Scripts/python.exe" my_tool.py
```

macOS note: TouchDesigner's code-signed Python can refuse *native*
extension modules (numpy, pillow, ...) when the venv interpreter is
launched standalone, even though the same packages import fine inside
TD. Pure-Python packages are unaffected.

## Pre-cook linking

TouchDesigner constructs COMP extensions lazily, with no ordering
guarantee between siblings -- so an extension doing a module-level
`import` of a venv package could construct *before* Embody had wired
`sys.path`, fail with `ModuleNotFoundError`, and silently lose every
promoted method until a `reinitextensions` pulse. TouchDesigner ships a
hook that runs strictly earlier: `app.pyEnvHelper` reads a context file
from the `.toe`'s folder and links the environment **before any COMP
cooks**.

Embody uses that channel. Once the venv is healthy it writes a minimal
`TDPyEnvManagerContext.yaml` next to the `.toe` (pointing at `.venv`,
with `autoSetup: false` so TD never races Embody's own provisioning),
records it in `.embody/manifest.json` like every other Embody-written
file, and adds an anchored `.gitignore` entry -- the file is
machine-local, like the venv itself, and TD normalizes it on first link.

The rules it follows:

- **Written only when no context exists, or the existing one is already
  Embody's.** A context you (or a fleet) authored is never modified,
  repointed, or deleted -- Embody just logs the co-existence notice
  below.
- **Removed automatically while the venv needs (re)install**, so TD
  never pre-cook-links stale packages; it returns after the next
  successful install.
- **Deleting the file opts out** -- Embody remembers the deletion and
  does not rewrite it on the next open. `op.Embody.InitEnvoy()`
  re-asserts it, and uninstall removes it with the rest of Embody's
  footprint.
- Requires a TouchDesigner build that ships `app.pyEnvHelper` (Embody
  feature-detects it; on older builds it silently keeps the
  extension-time wiring only).
- The pre-cook link covers `site-packages` imports and `Scripts` on
  `PATH`. Embody's own wiring still adds the pywin32 subdirectories and
  native DLL directories at extension init, so a module-level
  `import win32api` in a sibling extension may still need a deferred
  import.

## Working with tdPyEnvManager

TouchDesigner's palette component
[TDPyEnvManager](https://docs.derivative.ca/Palette:tdPyEnvManager)
solves an overlapping problem. The two co-exist safely in their default
configurations -- both use `.venv` next to the `.toe`, and Embody adopts
rather than fights it. Beyond authoring its own context when none exists
(see [Pre-cook linking](#pre-cook-linking)), Embody never mutates
another manager's setup: it checks at startup and logs:

- **Same `.venv`**: silent when the context is Embody's own (the healthy
  pre-cook state); an INFO note when a context you authored points at
  `.venv` -- the benign setup either way.
- **A different environment**: a WARNING -- Python modules will resolve
  from Embody's venv while native DLLs may resolve from the other
  environment, which produces confusing `DLL load failed` errors. Point
  tdPyEnvManager at `.venv` (its own default), or migrate: export its
  `requirements.txt` and feed the list to `InstallPackages`.
- **Conda mode**: Embody manages pip environments only; your conda
  environment stays under tdPyEnvManager's control.

One sharp edge worth knowing: setting `active: false` in
`TDPyEnvManagerContext.yaml` does **not** stop it from linking its
environment at startup. To fully hand off to Embody, remove the context
file (and the `[tool.touchdesigner.TDPyEnvManagerContext]` section of
`pyproject.toml` if present).

What Embody deliberately does *not* replicate: conda/Miniconda
environments, multiple named environments per project, and blocking
requirements installs during startup (Embody's installs are always
background).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError` right after `InstallPackages` | The install runs in the background -- watch the Embody log for the completion line first. |
| A warning says packages are "declared but not approved" | The declaration came from git (a teammate added it). Review it, then run `op.Embody.ApplyDeclaredExtras()`. |
| "has no pre-built wheel" refusal | Embody installs wheels only (source builds execute arbitrary code). Pick a version that ships wheels for TD's Python, or use a separate environment. |
| A package installs but errors on import with `DLL load failed` | Check the startup log for a tdPyEnvManager warning (two environments fighting), and confirm the package supports TD's Python version. |
| Extras missing on a teammate's machine | `.embody/project.json` wasn't committed, they haven't run `ApplyDeclaredExtras()`, or their Embody predates this feature. |
| "conflicts with the pinned environment" | Usually a genuine conflict with the MCP stack's pins. If the package worked before, the constraints snapshot may be stale -- delete `.venv/embody-core-constraints.txt` and retry. |
