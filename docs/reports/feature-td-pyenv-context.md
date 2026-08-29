# Feature brief: let TouchDesigner wire Embody's venv, pre-cook

**Filed** 2026-08-20 from node-pa-td. Companion to
[pyenv-coldstart-node-pa-td.md](pyenv-coldstart-node-pa-td.md), which is the incident
this removes. Every claim below carries file:line; TD paths are
`C:\Program Files\Derivative\TouchDesigner.2025.33070\bin`.

## The ask, in one paragraph

Embody already creates and owns a project venv at `project.folder/.venv` and installs
into it. The only defect is *when* that venv reaches `sys.path`: today it is wired from
`EmbodyExt._initPythonEnv()`, called out of `EmbodyExt.__init__` — Embody's own
extension construction. TouchDesigner constructs extensions lazily with no ordering
guarantee between sibling COMPs, so a hosted project's extension that does a
module-level third-party import can construct **first** and die on
`ModuleNotFoundError`. TouchDesigner ships a hook that runs strictly earlier — before
any COMP cooks — and it is driven entirely by a file. **Embody should write that file.**
Then `import anything_the_user_installed` works everywhere, including at the top of an
extension module, with no deferred-import discipline required of project authors.

## Why it matters (the failure this removes)

Observed live on node-pa-td: `/control/ControlExt` does
`from dotenv import dotenv_values` at module level. Its extension constructed before
Embody's, `dotenv` was not yet on `sys.path`, and the extension object came back
**`None`**. Nothing is promoted from a `None` extension, so the COMP lost every public
method and the project's whole REST API answered
`'td.containerCOMP' object has no attribute 'SearchLibrary'` — while `extensionsReady`
still read `True`. A `reinitextensions` pulse fixed it instantly, which is the
signature: the package was on disk the entire time.

The current mitigation is documentation — `docs/embody/python-environment.md:16-19`
tells project authors to defer their imports. That is 100% reliable and 0% enforceable,
and it does nothing for projects that already exist.

## The TouchDesigner contract (verified against the shipped source)

`app.pyEnvHelper` is stock TD: class in `bin/Lib/tdutils/TDPyEnvManagerHelper.py`, typed
on the app object at `bin/Lib/tdi/tdClasses/App.py:106`. The C++ side constructs it and
calls `postInit()` before any COMP cooks. **No COMP is required** — the palette
component is only a client of it (`self.Helper = app.pyEnvHelper`); the link happens
before any COMP exists, so no COMP *can* be required.

`postInit()` (`:91`) resolves three candidates against **`pathlib.Path.cwd()`** (`:100-103`)
in this precedence — note pyproject **wins**:

| Order | Source | Code |
|---|---|---|
| 1 | `pyproject.toml` → `[tool.touchdesigner.TDPyEnvManagerContext]` | `:114-118` via `loadContextFromPyproject` (`:1758`) |
| 2 | `TDPyEnvManagerContext.yaml` | `:120-125` |
| 3 | legacy `TDPyEnvManagerContext.json` (migrated to yaml on sight) | `:106-109` |

Once a context loads, `linkEnv(self.envPath)` (`:1255`) requires **both** of these to
exist or it returns `False` and logs an ERROR:

- `<envPath>/Scripts/python.exe` (Windows) · `<envPath>/bin/python` (posix)
- `<envPath>/Lib/site-packages` (Windows) · `<envPath>/lib/python{pythonVersion}/site-packages` (posix)

On success it prepends site-packages to `sys.path`, prepends `Scripts`/`bin` to `PATH`,
and sets `VIRTUAL_ENV`.

### Three contract details that will bite an implementer

1. **`envPath` in the file is ignored.** `applyContextDict` (`:1720`) recomputes it at
   `:1740` as `getPythonEnvPath(envName)` — i.e. from `envName` + `installPath`. Write
   `envName` and `installPath`, never a full path.
2. **`active = false` does NOT stop the link.** It only sets `startAsActive` (`:1735`);
   `postInit` links whenever a context loaded (`:137-139`). To hand off, the context must
   be *absent*, not inactive.
3. **The helper writes back.** After a successful link (`:1244-1245`) it calls
   `WriteContextToFile(Path.cwd()/'TDPyEnvManagerContext.yaml')`, which tries
   `WriteContextToPyproject` first (`:1862`) and re-serializes `installPath`
   **CWD-relative**. Consequences: any comment placed inside the section is destroyed,
   and a tracked file gets dirtied on every launch. Note also that TD's own
   `WriteContextToPyproject` only *updates a section that already exists* — it will not
   create one, so Embody must author the section itself.

The minimal section for an Embody venv at `<ctx dir>/.venv`:

```toml
[tool.touchdesigner.TDPyEnvManagerContext]
contextVersion = 2
mode = "Python vEnv"
envName = ".venv"
installPath = "."
pythonVersion = "3.11"
autoSetup = false
```

`autoSetup = false` is important: Embody owns provisioning, and TD's auto-setup
(`:131-136`) must never race it.

## Where this hooks into Embody

Two sites, both needed — one covers creation, one covers every later open:

| Hook | Location | Covers |
|---|---|---|
| `install_dependencies()` success epilogue | `embody_pyenv.py:910-913`, beside `write_env_stamp` | every venv create/rebuild; worker-safe, pure filesystem |
| `_initPythonEnv()` healthy branch | `EmbodyExt.py:840-848` | every subsequent open, including venvs that predate this feature; main thread; already runs the tdPyEnvManager detection |

Supporting facts: `venv_paths()` (`embody_pyenv.py:80`) is the single spec builder —
`venv_dir` at `:91`, `site_packages` at `:109`/`:114`. `environment_needs_install()`
(`:227`) is the health ladder. Writing project-level files is well-precedented and
already has machinery to reuse: Embody writes `.gitignore`, `.gitattributes`, `.mcp.json`,
`AGENTS.md`, `CLAUDE.md`, `opencode.json` and `.claude/**`, all footprint-tracked in
`.embody/manifest.json` and consent-gated through `_guardFileWrite`. **This feature
should go through the same guard and be recorded in the same manifest** — it is a file in
the user's project, and the user must be able to see it and take it back.

## The hard problem: which directory?

TD resolves the context against `Path.cwd()`, and Embody's launch surfaces do not agree
on what that is:

- The MCP bridge passes **no `cwd=` at all** — TD inherits the bridge's CWD
  (`text_envoy_bridge.py:3876-3880`; `cwd` appears twice in 6154 lines, both `os.getcwd()`).
- Convoy's fleet launcher sets `cwd` to **`project_root`**
  (`convoy/host/convoy_lifecycle.py:1438`).
- The venv lives at **`project.folder`** — the `.toe`'s folder, not the root. In Embody's
  own repo those differ (`<root>/dev` vs `<root>`).
- On macOS the bridge spawns via `open -n -a` (`:3835`), so a `cwd=` would apply to
  `open`, not to TouchDesigner — LaunchServices does not propagate it.

There is already evidence of the resulting artifact: `.gitignore:61` ignores
`dev/TDPyEnvManagerContext.yaml`, a TD-generated file that landed in `project.folder`
rather than the root.

**Recommended resolution:** exploit the fact that `installPath` is resolved relative to
the context's own location (`applyContextDict:1738-1740`). Write the context wherever it
must live and point it at the venv with a *relative* `installPath` — e.g. a context at
the git root for a `.toe` in `dev/` carries `installPath = "dev"`, `envName = ".venv"`.
That decouples "where the file is found" from "where the venv is", which is the crux.
Then either write it in both candidate directories, or pin the CWD on the launch paths
Embody controls and accept the file in one known place. Whichever is chosen, decide it
explicitly — the current implicit CWD is the reason this needs a decision at all.

Also note there is **no stdlib TOML writer** (Python 3.11 has `tomllib` for reading
only, and TD ships it at `bin/Lib/tomllib/`). Embody has no `tomli_w` dependency, and
PyYAML lives inside the very venv being bootstrapped. So serializing the section is
hand-rolled — which argues for the YAML channel, or for a deliberately tiny hand-written
TOML emitter for these six scalar keys.

## What must not change

- **Never clobber a human-authored context pointing somewhere else.** Embody's current
  stance is read-only co-existence — the banner at `embody_pyenv.py:1699-1702`
  ("read-only, warn-don't-mutate") and the `detect_tdpyenvmanager` docstring
  (`:1706-1707`). This feature narrows that stance to *"write only when no context
  exists, or when the existing one is already ours"*. It must not silently repoint a
  context a user created — node-pa-td's own outage was caused by exactly that class of
  edit, from the other direction.
- Keep the existing `_initPythonEnv` wiring as the fallback. The context channel only
  helps TD instances launched after it is written; the extension-time wiring remains the
  safety net for the first run and for anyone who deletes the file.

## Acceptance criteria

1. Fresh Embody project, venv created, TD **cold-opened** (not re-cooked): a COMP whose
   extension does `import <package installed only in .venv>` at module level constructs
   successfully.
2. `%LOCALAPPDATA%\Derivative\TouchDesigner099\TDLogs\TDPyEnvManagerHelper_<pid>.log`
   **does not exist** for that session. This is the cheapest possible check and it is
   inverted: success logs at INFO under a level-30 logger (`:155-160`), so the file's
   existence means the link was *refused*.
3. A pre-existing context pointing at a different env is left untouched, and the current
   warning still fires.
4. Deleting the context file returns the project to today's behaviour, with no errors.
5. The written file appears in `.embody/manifest.json` and can be reverted through the
   same path as every other Embody-written project file.

## Open questions for the implementer

- Which TD build added the `pyproject.toml` channel? node-pa-td's fleet must not be
  ahead of it. `2025.33070` has it; the floor is unknown from these repos.
- What CWD does each editor's stdio MCP host actually hand the bridge? The chain points
  at the workspace root, but it was not verified.
- Does the write-back at `:1244-1245` fire on *every* launch? If so, a tracked context
  file will show as dirty routinely, and it should probably be gitignored like
  `TDPyEnvManagerContext.json` already is.
