# Cold-start venv wiring: a field report from node-pa-td

**Filed** 2026-08-20 from the node-pa-td staging Control (TouchDesigner 2025.33070,
Windows 11). Investigated read-only across both repos by four parallel readers plus
two adversarial passes; every claim carries file:line or a git sha.

**One-line summary:** a config regression disabled TouchDesigner's own pre-cook venv
link, and with only Embody's extension-time wiring left, a hosted project's extension
lost the documented race and failed to construct -- taking its entire promoted API
down with it, silently.

---

## 1. What breaks

On a cold open of a hosted TouchDesigner project, a COMP extension whose module DAT does a **module-level import of a package that lives only in a project venv** fails to construct, because that venv's `site-packages` is not on `sys.path` at the instant TD imports the module. In node-pa-td this is `/control/ControlExt`, which does `from dotenv import dotenv_values` at `control/ControlExt.py:27`; the traceback surfaces as `ModuleNotFoundError: No module named 'dotenv'` raised through TD's import hook. The consequence is not a warning — the extension object is `None`, so **nothing is promoted**: `op('/control').Role`, `.SearchLibrary` and every other public method vanish while `extensionsReady` still reads `True`, and the project's REST API answers `'td.containerCOMP' object has no attribute 'SearchLibrary'`. Pulsing `/control par.reinitextensions` repairs it completely (verified live: `logs/Control.36.toe_260820.log:45-54`). The same exposure exists at `render/RenderExt.py:14` (identical import, every render machine) and `control/lib/dropbox/DropboxExt.py:1-5` + `:18` (`dropbox`, `requests`), where the failure is additionally **silent** — `control/ControlExt.py:160-163` swallows the resulting `AttributeError`, and `control/networking/webserver_api_callbacks.py:4504-4511` treats a missing Dropbox extension as a supported configuration.

## 2. Reproduction

Established conditions of the observed failure:

- Host: Windows 11, TouchDesigner **2025.33070**, Python 3.11.15.
- `Control.36.toe` launched by Envoy's `launch_td`; process **PID 1996**, started `2026-08-19 23:59:09` (`Get-Process` `StartTime`), mapped in `.embody/envoy.json` as `"Control.36": {"td_pid": 1996}`. `Render.36` = PID 41880, started 23:54:02.
- Two venvs exist in the project root: `.venv` (Embody-managed) and `node-pa-td_vEnv` (tdPyEnvManager). `dotenv` is present in **both**; `dropbox` and `requests` only in `node-pa-td_vEnv` (verified by directory listing).

Minimal generic repro for Embody-side work: a COMP with an extension whose source DAT imports, at module level, a package present only in the project venv; cold-open the `.toe` (not a re-cook, not `reinitextensions`). Success on re-cook and failure on cold open is the signature.

## 3. Root cause, as best established

**Established fact — the TD-native venv link was refused for the whole life of the failing process.** TouchDesigner's own helper log for PID 1996 records:

```
2026-08-19 23:59:13,123 - ERROR - ... Python executable not found at
  C:\Users\admin\Documents\Git\node-pa-td\node-touchdesigner_vEnv\Scripts\python.exe.
2026-08-19 23:59:13,124 - ERROR - Environment ...node-touchdesigner_vEnv could not be linked.
```
(`%LOCALAPPDATA%\Derivative\TouchDesigner099\TDLogs\TDPyEnvManagerHelper_1996.log`; PID 41880 identical at 23:54:05.) That folder does not exist on the box. The refusal path is `bin/Lib/tdutils/TDPyEnvManagerHelper.py:1273-1275` — `return False` **before** the `addToSysPath` call at `:1283` — so `node-pa-td_vEnv` was never added to `sys.path` in that process, early or late. Cause: commit **bea8996** (`TEC Installs`, 2026-08-19 07:42:45, *"fixed venv name to match new folder name"*) changed `envName: node-pa-td_vEnv` → `node-touchdesigner_vEnv` in `TDPyEnvManagerContext.yaml`. **HEAD still carries the broken value**; the correction exists only as an uncommitted working-tree edit (`git status`: ` M TDPyEnvManagerContext.yaml`). The project's own provisioner predicted this failure verbatim at `!!_provision.ps1:75-78`: *"a clone in a folder named anything but node-pa-td must still use the name in the YAML, or TD starts without the venv and both extensions die at Init on ModuleNotFoundError: dotenv."*

**The `previousimport` frame is resolved and is a red herring — TouchDesigner owns that hook, not Embody and not node-pa-td.** `C:\Program Files\Derivative\TouchDesigner.2025.33070\bin\TouchInit.py:43-50`:

```python
previousimport = __builtins__.__import__
def tdcustomimport(*args, **kw):
	r = importDAT(args[0]);
	if not r:
		r = previousimport(*args, **kw)
	return r;
__builtins__.__import__ = tdcustomimport
```

It is present in every TD session ever started; `previousimport` appears nowhere in either the Embody or node-pa-td trees. This also **rules out DAT shadowing**: `importDAT('dotenv')` is tried first, so a DAT named `dotenv` would have *succeeded*. A genuine `ModuleNotFoundError` falling out of the builtin fallback means the name resolved nowhere on `sys.path`.

**Explicitly uncertain — whether a second, independent ordering failure exists.** The evidence is contradictory and I could not settle it read-only:

- Embody's warning at `logs/Control.36.toe_260820.log:1` (07:43:50, frame 0) names `node-pa-td_vEnv` explicitly. `embody_pyenv.py:1744-1752` only populates `env_path` when `helper.HasLinkedCustomEnv` is True — so on that load **the tdPyEnvManager link was healthy**.
- On that same load, `ControlExecute.py:8-9` (`onStart` → `op.App.Init()`) produced **no** `Control Init, role:` line (`control/ControlExt.py:88`) until 07:49:39, and the investigator pulsed `reinitextensions` at 07:47:35 — consistent with ControlExt failing to construct *again*, on a healthy link.
- Yet `Render.36` re-opened at 07:50:49 with the same healthy link and its extensions initialized normally at frame 0 (`logs/FLEX_1_2026-08-20.log:1-6`), despite `render/RenderExt.py:14` carrying the identical import.

Note also that **every diagnostic quoted in the original brief was captured in the 07:43:50 session, not the 23:59 one** (`logs/Control.36.toe_260820.log:11-47`: `get_op_errors` 07:46:22, the `extensionsReady`/`hasSearchLibrary` probe 07:46:24, the `import dotenv` probe 07:47:16, the reinit pulse 07:47:35, the `sys.path` snapshot 07:52:51). The 11-entry `sys.path` is therefore a **post-repair** snapshot, and its ordering (`node-pa-td_vEnv` at index 5, below `.venv`'s trio at 2-4) is exactly what you get when the helper prepends first and Embody's three `insert(0, …)` calls push it down — it is not evidence about the 23:59 load.

## 4. Why the current handler does not cover this case

`EmbodyExt._initPythonEnv()` (`dev/embody/Embody/EmbodyExt.py:825`) is called from `EmbodyExt.__init__` at `EmbodyExt.py:390` — i.e. at **Embody's own extension construction**. TD gives no ordering guarantee between one COMP's extension construction and another's, and Embody documents exactly that at `docs/embody/python-environment.md:12-19`: *"TouchDesigner does not guarantee that other components initialize after Embody, so an extension in a sibling COMP with a module-level import may still want a deferred import for the very first cold open."* `EmbodyExt.py:828-829` concedes the asymmetry: *"tdPyEnvManager links pre-cook and Embody historically waited for Envoy Start (frame 30+)."*

Structurally, Embody has no hook that can run earlier:

- No preferences write (`app.preferencesFolder` is read-only usage in `shortcuts.py:318,325`).
- `PYTHONPATH` is deliberately **stripped**, not set, for child processes (`embody_pyenv.py:534-556`).
- The bridge launches TD with neither `cwd=` nor `env=` (`dev/embody/envoy_bridge.py:3876-3880`), so the launch path cannot seed anything either.
- The older wiring path (`EnvoyExt.py:5073,5088`) runs on the bootstrap worker at frame 30+, far too late.

Two secondary gaps worth fixing regardless of the ordering decision:

- **The skip is silent.** `_initPythonEnv` gates on `os.path.isdir(spec['site_packages']) and not pyenv.environment_needs_install(spec)` (`EmbodyExt.py:838-842`); `wire_python_paths` returns `False` with no log on either failure branch (`embody_pyenv.py:417-421`). A project whose venv is missing or stale gets no diagnostic at all.
- **Version at the failing load is unresolved** (see §7) — no file in node-pa-td records the installed Embody version.

## 5. What a fix must satisfy

**The constraint:** the venv's `site-packages` must be on `sys.path` before TD imports *any* hosted extension module. That happens at COMP extension construction, which TD performs **lazily** — Embody itself hit this and documented it (`EmbodyExt.py:9629-9634`, `docs/changelog.md:17`). So "early enough" cannot mean "early in Embody's own init"; it has to mean *before any COMP cooks*, or else the failure has to be repaired after the fact.

| Option | Mechanism | Trade-off |
|---|---|---|
| **A. Pre-construction injection** | Make the paths exist before TD builds any COMP: pass `env={**os.environ, 'PYTHONPATH': site_packages}` on the TD spawn at `envoy_bridge.py:3876-3880`, or adopt TD's own pre-cook channel by writing/repairing a `TDPyEnvManagerContext.yaml` pointing at `.venv`. | The only option that actually closes the race for a cold open. But it only covers TD instances Embody launches (a user double-clicking the `.toe` gets nothing), it contradicts Embody's deliberate `PYTHONPATH`-stripping policy (`embody_pyenv.py:534-556`), and the YAML variant breaks Embody's stated read-only co-existence contract (`embody_pyenv.py:1705-1706`, `docs/embody/python-environment.md:139-171`). |
| **B. Idempotent wiring + post-injection reinit** | Keep wiring at init (it is already idempotent — `add_site_packages` at `embody_pyenv.py:334-337` is guarded by `p not in sys.path`), then, once wired, scan for COMPs whose `.extensions` is empty despite a non-empty extension parameter and pulse `reinitextensions` on them. | Works retroactively and needs no cooperation from the host project. Two real costs: (i) reinit destroys deliberately non-persisted in-memory state — see the comment block at `control/ControlExt.py:35-54` explaining why `CanvasArtworks`/`CanvasPaused` must *not* survive a reinit; doing this at frame 30 after a wall has begun playing would silently wipe transport truth. So it must fire in the first few frames, before real state exists, and only on extensions that are genuinely `None`. (ii) It cannot distinguish "failed for want of a package" from "failed for a code bug", so it should log loudly rather than repair invisibly. |
| **C. Documented requirement: hosted projects defer third-party imports** | Already Embody's position (`docs/embody/python-environment.md:16-19`; shipped into every project's CLAUDE.md at `dev/embody/Embody/templates/text_claude.md:103-120`). | 100% reliable and zero-risk, but depends on every project author and does nothing for existing projects. node-pa-td has the pattern in-repo already — `control/lib/dropbox/DropboxExt.py:28-34` (try/except + fallback) and `:1205-1209` (try/except + operator-visible log) sit eleven lines below the unguarded `import dropbox` that does not. |

My read: **B + C as the shipped combination, with A(i) as an opt-in for Embody-launched instances**, plus a loud log on the currently-silent skip path (`embody_pyenv.py:417-421`). B makes the failure self-healing where it happens; C is the only thing that makes it impossible; A closes the window for the launch path Embody controls.

## 6. The tdPyEnvManager question — recommendation for node-pa-td

**Do not remove it, and do not treat this incident as evidence against it.** Both adversarial passes refuted the premise: tdPyEnvManager did not lose a race here, it was never given a valid target. Concretely, removal today would cause immediate, non-racy breakage:

- `dropbox` and `requests` exist **only** in `node-pa-td_vEnv` (`.venv/Lib/site-packages/dropbox` and `/requests`: *No such file*; TD does not bundle `dropbox`). `control/lib/dropbox/DropboxExt.py:1-5,18` imports both at module level, and the failure is swallowed at `control/ControlExt.py:160-163`.
- `python-dotenv` is in `.venv` only **transitively** — `.venv/embody-env.json` declares `["attrs<25","cryptography>=3.4","mcp>=2.0.0,<3","pywin32>=311","pyyaml"]`; the package arrives via `mcp`/`pydantic-settings`/`uvicorn`, at 1.2.2 against a `requirements.txt` pin of 1.2.1.
- `.embody/project.json` has **no `python` key at all** (only a `convoy` block), so zero extras are declared — and that file is gitignored (`git check-ignore -v` → `.gitignore:140`), so declared extras would not reach render machines via `git-update`. `TDPyEnvManagerContext.yaml` is tracked and does travel.
- `!!_provision.ps1:79-93` derives the venv name from that YAML; deleting it silently switches provisioning fleet-wide to a folder-derived name.

Recommended node-pa-td actions, in order:

1. **Commit a correct `envName` and standardize the folder name across machines.** HEAD currently ships the outage to every machine that pulls. Naively reverting to `node-pa-td_vEnv` re-breaks TEC-B4A, whose repo folder is `node-touchdesigner` (that is why bea8996 exists) — so this needs a fleet decision, not a blind revert.
2. **Make the three unguarded module-level imports lazy** — `control/ControlExt.py:27` (used only at `:79` and `:467`), `render/RenderExt.py:14` (only `:70`, `:346`), `control/lib/dropbox/DropboxExt.py:1-5`. Every use site is already inside a method, and the guarded pattern already exists in the same file. This removes the dependency on *any* venv manager's timing.
3. Consolidating to one venv is a defensible *future* goal — the current split is exactly Embody's `different_env` warning case (`embody_pyenv.py:1775-1780`, and the live warning at `logs/Control.36.toe_260820.log:1`) — but it requires declaring `dropbox` and `python-dotenv` first, solving the gitignored-config distribution problem, and confirming a new-enough Embody on every render.

I am not claiming tdPyEnvManager is *sufficient* — see the unresolved 07:43:50 evidence in §3. I am claiming that removing it today is strictly subtractive.

## 7. What the Embody agent should verify first

1. **Does a hosted extension with a module-level venv import construct successfully on a cold open when tdPyEnvManager's link is healthy?** This is the fork between "one config regression" and "a real ordering hole", and the 07:43:50 load points both ways (§3). Discriminating test, on an idle box: instrument the top of `control/ControlExt.py` (before line 27) with a probe that appends `sys.path` and `importlib.util.find_spec('dotenv')` to a file — that line executes at the exact instant TD imports the module — then cold-launch three ways: (a) corrected YAML, Embody bypassed; (b) both context files deleted, Embody ≥ v6.0.259; (c) both active. Note that the helper's file handler is created lazily and success logs at INFO under a level-30 logger (`TDPyEnvManagerHelper.py:161-164`, success line at `:1241`), so **the mere existence of a `TDPyEnvManagerHelper_<pid>.log` means the link was refused.**
2. **Why did the 23:59 session write no Embody log at all?** `logs/Control.36.toe_260819.log` stops at 23:52:24 (mtime 23:52) and `logs/Control.36.toe_260820.log` begins at 07:43:50 frame 0 — yet PID 1996 ran continuously from 23:59:09. So Embody's own startup sequence for that open (the `execute:10: Embody vX` line at `dev/embody/Embody/execute.py`) is entirely missing. Either Embody's logging did not come up, or the frame counter/log rotation behaves differently than I assume across an in-process project re-open. Worth explaining on the Embody side; it also means the deployed version at the failing load **cannot be read from disk** (`.embody/local.json` holds only `{"td_build": "2025.33070"}`), so the v6.0.246-vs-v6.0.259 question stays open. `Control.36.toe` mtime is `Aug 19 19:54`, before the `.embody/updates/backup-v6.0.246.tox` write at 23:26.
3. **Confirm the `app.pyEnvHelper` pre-cook timing claim independently.** Embody asserts it at `embody_pyenv.py:1707-1710` and `EmbodyExt.py:828`, attributed to a 2026-08-19 read of the shipped source. I read `TDPyEnvManagerHelper.py` and confirmed `postInit` (`:91-145`) reads the context from `pathlib.Path.cwd()` (`:100-103`) and links via `linkEnv` (`:137-139`), and that `addToSysPath` **prepends** (`:1153-1160`) — but the *caller* of `postInit` lives in TD's C++ binary (`bin/Lib/tdi/tdClasses/App.py:106` is only a type stub), so the "before any COMP cooks" ordering itself remains unverified from source.
4. **Close the silent-skip path.** `wire_python_paths` returning `False` (`embody_pyenv.py:417-421`) with no log is the state a project in this failure mode is most likely to be in; it should be the loudest thing in the boot log, not the quietest.
5. **Note the cwd dependency for anything that touches TD's helper:** `postInit` resolves context files against the process CWD, and `envoy_bridge.py:3876-3880` spawns TD with no `cwd=`. In this incident the CWD happened to be correct (the helper resolved `installPath: '.'` to the repo root and logged it), but it is an unpinned assumption in Embody's launch path.

---

## 8. Addendum: §3's open fork is CLOSED -- the link was dead at the failing load

Section 3 could not decide whether a second, independent ordering hole exists, because
the 07:43:50 load appeared to have a healthy tdPyEnvManager link while ControlExt still
failed. Three facts gathered after the report was written resolve it, and they change
the conclusion from "unresolved" to "both mechanisms are accounted for".

**1. TD's helper is one-shot at PROCESS start, and it ran with the broken name.**
`TDPyEnvManagerHelper_1996.log` contains exactly two lines, both at `23:59:13` -- the
refusal. There is no later entry. The helper's `postInit` never re-ran for that process,
so `node-pa-td_vEnv` was absent from `sys.path` for the entire life of PID 1996,
including at the 07:43:50 project load.

**2. The context file was corrected three seconds before that load.**
`TDPyEnvManagerContext.yaml` mtime is `2026-08-20 07:43:47`; the load logged frame 0 at
`07:43:50`. So Embody read a file that had *just* been fixed, while TD's helper had
already refused the *old* value 7h44m earlier.

**3. Embody's "linked to a DIFFERENT environment" warning is FILE-derived, not
link-derived.** That is why it names `node-pa-td_vEnv` on a load where nothing was
linked. Anything in Embody that reasons about tdPyEnvManager's state should treat the
context file as a statement of intent, not evidence of a live link. The authoritative
signal is TD's own helper log -- and note its inversion: because success logs at INFO
under a level-30 logger, **the existence of a `TDPyEnvManagerHelper_<pid>.log` file
means the link was REFUSED.** Silence means success.

**Why the 23:59 session wrote no Embody log (open question §7.2):** the process started
at `23:59:09` but the project load did not begin until `07:43:50`, because TouchDesigner
was blocked on a modal **"New Plugin Detected"** dialog for `plugins/Moonbeamout.dll`
(rebuilt 2026-08-18, so its hash no longer matched the trust list). A human cleared it
the next morning. Nothing was wrong with Embody's logging: there was simply no project
load to log. Worth knowing generally -- an Embody-launched TD can sit at a modal
indefinitely with a live process, a bound Envoy port, and no project.

### What this means for the ordering question

At the 07:43:50 load, tdPyEnvManager was effectively *removed* (refused), leaving
Embody's `_initPythonEnv` as the only wiring mechanism -- and `/control`'s extension
construction beat it. `ControlExt` raised `ModuleNotFoundError: No module named
'dotenv'` while `.venv/Lib/site-packages/dotenv` existed on disk the whole time; a
`reinitextensions` pulse minutes later imported it without incident.

So the ordering hole in §4 is not theoretical. It was demonstrated, on this box, the
moment the pre-cook mechanism stopped covering for it. That is the strongest available
argument for §5's option B or A: today, Embody's handler is load-bearing only when
tdPyEnvManager is healthy, which is precisely when it is not needed.

### Fixed on the node-pa-td side already

- `9476da4` restores `envName: node-pa-td_vEnv` (the fixed string `!!_provision.ps1`
  documents). TEC-B4A must provision its venv under that name rather than renaming the
  YAML again -- that ping-pong is what caused this.
- The three unguarded module-level imports (§6.2) are still open on the node-pa-td side.
