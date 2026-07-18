# Changelog

## v6.0.132

TD 2025.33070 palette-scan freeze + frame drops, fixed structurally: the palette scan no longer loads components into TD at all -- a background toeexpand worker reads types and child counts from unpacked .tox files with zero main-thread cost -- plus 33070 bootstrap rows, a poison-pill sentinel, and blocklist entries for the two components that wedge 33070.

- **Palette scan rebuilt on `toeexpand` -- zero frame drops, zero freeze surface**: on a bootstrap miss the scan now expands each palette `.tox` with TD's bundled `bin/toeexpand` on a WORKER thread (pure subprocess + file I/O, no TD access) and reads the placed component's type and child count from the expansion (both toeexpand output formats: new-style `.n` trees and old-style `.init` trees like `template.tox`); a `run()`-chain poller drains results on the main thread and reuses the existing checkpoint/resume machinery. Parser validated against loadTox-derived ground truth for every comparable 33070 component. Measured on the old path: 78 of the first 91 palette loads exceeded the 60fps frame budget (6.3s of main-thread `loadTox` in one-third of the scan). The new path does no per-component main-thread work, and since no palette component's init code ever executes, the entire class of load-time freezes cannot occur. Guardrails: a scan whose expansions ALL fail (e.g. toeexpand blocked by AV policy) falls back to the in-TD scan instead of finalizing an empty catalog; a dying poller cancels the worker via a stop event; stale worker temp dirs are swept at scan start. The legacy in-TD `loadTox` scan survives only as a fallback when toeexpand is missing, now hardened with `allowCooking=False` on the scan wrapper (palette per-frame executors can no longer fire during the census) and the freeze sentinel below. Note: `toeexpand` reports success with exit code 1 -- outcomes are judged by the expansion directory, never the return code.
- **geoPanel and chromaKey blocklisted -- the TD 2025.33070 wedge** : loading `Techniques/geoPanel.tox` wedges TouchDesigner 2025.33070's frame loop within one frame of `loadTox` RETURNING (the process stays alive and "Responding" but no frame ever advances -- UI, run() chains, and delayed callbacks all stop). Verified by isolation repro: the md5-identical file loads clean on 2025.32820 and wedges 2025.33070 every time -- a TD-side regression, reported upstream to Derivative. geoPanel ships per-frame `panel.interactTouch()`/`interactMouse()`/`setFocus()` Execute-DAT callbacks that fire the moment the network exists, plus Leap Motion SDK init and a hard-coded LAN Touch In. `Tools/chromaKey.tox` wedges 33070 the same way (full-palette probe with geoPanel excluded stalled right after it). On a fresh 33070 install the scan froze at `Scanning palette (89/251)`; because checkpoint-resume (v6.0.128) resumed straight back into the same component, every relaunch froze again.
- **In-flight sentinel convicts freeze-causing components automatically**: the (fallback) scan writes `.embody/palette_scan_inflight.json` naming each palette component right before loading it, removing it only on clean outcomes (scan finalize, graceful abort, extension teardown). A launch that finds a sentinel knows the previous session was killed or wedged mid-scan, permanently skips the named component for that build (persisted under the reserved `_palette_blocked` catalog key, carried through checkpoints), and logs what happened. Any future freezing palette component self-heals on relaunch instead of freeze-looping -- the sentinel survives until the NEXT component's write, and the fallback scan now spaces chunks 3 frames apart so wedges landing 1-2 frames after `loadTox` returns (exactly geoPanel's failure mode) are attributed to the right component, not its innocent successor.
- **Bootstrap palette rows for TD 2025.33070**: `palette_catalog` now ships rows for build 099.2025.33070 (extracted via toeexpand from the 33070 palette, including the new POPs Gaussian Splats components and both wedge-causing components -- toeexpand executes nothing, so even those are safely cataloged), so fresh installs on the current official TD build skip the runtime scan entirely.
- **Tests**: new `TestCatalogPaletteSentinel` (9 tests) and `TestCatalogToeexpandScan` (9 tests) suites cover the sentinel round-trip and conviction flow, the expansion parser (type mapping, child census, ambiguity rejection), a real end-to-end toeexpand worker run, scan routing, and the poller's drain/finalize/fatal-fallback paths.

## v6.0.131

Multi-instance bridge correctness (issue #57 follow-up): instance-aware process liveness, restart_td can no longer quit the wrong TouchDesigner, plus a Windows TDN-rename file leak and the pre-existing Windows test failures fixed.

- **Bridge liveness is instance-aware**: the bridge inferred "TouchDesigner is running" from ANY TD process on the machine (`find_td_pid()` = first process matching the image name). With several projects open, a dead instance read as alive, `crash_detected` never fired, and `launch_td` refused to relaunch. Liveness is now: the active instance's REGISTERED pid (image-verified via `is_td_process_alive`, so an OS-recycled pid cannot false-match) or its registered port answering. A dead registered pid resolves to `None` -- never to a stranger's pid. `TouchDesignerWebRender` helpers are excluded from process discovery on Windows.
- **`restart_td` targets only the active instance**: it previously quit `find_td_pid()` -- literally the first TouchDesigner on the machine, which with multiple projects open could terminate a DIFFERENT project's TD. It now resolves the active instance's verified pid from the registry (state fallback, also verified) and refuses with a clear message when that instance isn't running, ignoring unrelated TD processes.
- **TDN rename no longer leaks the old file on Windows** : `_updateMovedTDNOp` used `Path.rename()`, which overwrites on macOS but raises `FileExistsError` on Windows when Embody's own sweep already exported the new-name `.tdn` -- every rename left the old file behind (`Error renaming TDN file` in logs). Now `Path.replace()` for identical cross-platform overwrite semantics.
- **Launch scripts normalize to forward slashes**: `str(Path)` flips to backslashes on Windows hosts, producing invalid zsh `cd` paths in the macOS `.command` script and platform-dependent `.bat` content. Both generators (and the invoked CLI path) now emit forward slashes; verified `cmd.exe` accepts quoted forward-slash paths for `cd /d` and program invocation.
- **Windows test-suite health**: `test_tdn_crash_safety.test_A04` skips on Windows (chmod cannot write-protect a directory there); `test_resolve_toe_path_relative` builds its expectation portably (abspath adds a drive on Windows); watchdog tests no longer signal the LIVE server's shutdown event (a full-run server bounce); the stuck-start revive test pins an expired startup deadline; and the `test_shortcuts` parexec tests pin the parexec suppression gate open (`_restoring_settings` / `_init_complete` are live shared state other suites toggle mid-run -- the order-dependency that made them fail only in full runs). Full suite on Windows: **2085/2092 passed, 0 failed, 7 skipped** (the skips are the Unix-only pgrep tests plus the Windows chmod skip).

## v6.0.130

Windows MCP transport fix (issue #57): bridge targets 127.0.0.1, restart-storm guards, delete_op tracking purge -- plus the Envoy watchdog false-revive fix and MCP test-runner status hardening (issue #60 follow-up).

- **Bridge targets `127.0.0.1`, never `localhost`** (fixes [#57](https://github.com/dylanroscover/Embody/issues/57)): the STDIO bridge forwarded every request to `http://localhost:<port>/mcp`. Windows resolves `localhost` to `::1` first, Envoy binds IPv4-only, and `urllib` tries addresses sequentially -- on hosts whose firewall stealth-drops loopback SYNs (measured: ~2.0s to refuse a closed loopback port that healthy Windows refuses in <1ms), every single MCP call burned ~2s on the doomed IPv6 attempt, and a full drop becomes the reported multi-minute "create_op hangs" (the bridge forward timeout is 300s). All bridge URL sites, the `envoy_setup` HTTP-fallback `.mcp.json`, and the disk-fallback bridge copy now target `127.0.0.1` explicitly. Measured on the reporter's host class: 2.1s -> 0.07-0.27s per call.
- **Envoy no longer restart-storms when its port is contested**: a first startup failure with the configured base port held by another process entered a self-sustaining loop (observed: 575 attempts over 30 minutes, continuing even after "Envoy disabled") -- stacked restart `run()`s all eventually fired, the watchdog revive cleared the duplicate-start guard and signaled the in-flight worker's shutdown event, and `_forceCloseOldServer` killed newborn servers via the global handle with no ownership check. Now: `Start()` gates on `Envoyenable`; queued restarts are generation-stamped and go stale when superseded; the revive skips starts inside their startup window; force-close verifies the handle's generation before signaling or closing anything; and a worker that finds its shutdown event pre-set at startup logs a loud dead-on-arrival WARNING with generation/lifetime detail instead of looping silently. Verified live: contested base port now recovers in one clean scan-and-bind.
- **`delete_op` purges tracking for every strategy**: deleting an externalized DAT (or tox/json/... op) left its `externalizations.tsv` row and on-disk file behind until a Refresh sweep reclaimed the row (the file never). `_purgeTDNTracking` is now `_purgeExternalizationTracking`: rows for the op and tracked descendants are removed synchronously for all strategies, files are deleted on the same deferred schedule as the TDN path -- guarded by the clone-tag and shared-file-reference checks (`_checkFileReferences`) so a file another live op still uses is preserved.
- **Bridge `tools/list` augmentation is idempotent**: the shipped bridge template appended its meta-tools (`get_td_status`, `launch_td`, ...) without checking for duplicates -- the guard existed only in the repo's disk-fallback copy, which had silently drifted ahead of the template that actually deploys. The guard is forward-ported to the template and the two copies are re-synced byte-identical.
- **Watchdog no longer revives a healthy cold start**: the liveness watchdog treated `Preparing Python environment...` (the fast-path import gate warming the MCP Python stack on a worker thread) as a settled state, probed the not-yet-bound socket, and force-revived the in-flight startup ~8s in -- observed 7 seconds after launch on a cold open. The status is now classified as transitional, so a slow first import gets the same ~24s stuck-grace as `Starting...`/`Restarting...`/`Reviving...`, and a genuinely orphaned import gate (extension reinit mid-warmup) still self-heals via the grace-path restart.
- **Overlapping `run_tests` calls are refused**: a second MCP `run_tests` while one was active captured `Testing` as the "prior" Embody Status and restored that lie after the run (Status stuck at `Testing` forever), while overwriting the first caller's completion handle (30s transport timeout). The tool now refuses overlapping runs cleanly, keeps the prior Status in COMP storage so it survives an extension reinit mid-run, never captures the literal `Testing`, and the completion poll restores Status even when the pending handle was lost.
- **`test_smoke_release.test_status_enabled` fixed**: it read the live Status par, which the MCP runner holds at `Testing` for the entire run -- a deterministic failure on every MCP-invoked run (misread as a revive race during the v6.0.128 release run). It now asserts against the saved prior status; a genuinely stuck `Testing` still fails loud. Watchdog suite +5 tests: `Preparing` transitional classification, stuck-gate grace restart, and the storage-backed status-restore contract.

## v6.0.128

Issue #60 (default-startup-file freezes, timeline fighting, prompt nagging): five root-cause fixes across the catalog scan, the dropped-.tox sweep, settings persistence, and the Envoy venv probe -- plus a new shipped `worktree-td-safety` rule.

- **Palette scan stops fighting the user's timeline** (fixes [#60](https://github.com/dylanroscover/Embody/issues/60)): the first-launch catalog scan snapshotted timeline state once at scan start and force-restored it after every chunk, so pausing mid-scan was un-paused over and over. The snapshot/restore bracket is now per-chunk: a user pause (or rate/cookRate/realTime change) between chunks is adopted, while a palette component's own mutations inside the chunk are still undone.
- **Catalog scan checkpoints and resumes**: the catalog was only written at the very end of the full scan, so closing TD mid-scan restarted it from zero on every launch, forever. The op-type half is now written before the palette phase, palette results checkpoint every 25 components (`_palette_partial` marker), writes are atomic (tmp + `os.replace`), and the next launch resumes where the scan left off (deferred past the frame 30-90 restore phases). A scan can no longer wedge itself (`_scan_in_flight` clears on failure), silently re-enable a Disabled Embody (`_setScanStatus` guard), or crash the cross-build patcher on a checkpointed catalog (`_findShiftedDefaults` skips reserved keys).
- **`tdn_exclude` silences the dropped-.tox sweep, ancestry-wide** (fixes [#60](https://github.com/dylanroscover/Embody/issues/60)): the "Dropped .tox Expression Detected" dialog never consulted the exclude tag; it now skips tagged COMPs and their whole subtrees (`_hasExcludeTagInAncestry`), as does Externalize Full Project (`_shouldSkipOp`). A plain Ignore is remembered for the session; `Toxdropexpr` is persisted to config.json so "Always" answers survive into new untitled projects (which reload baked `.toe` defaults and previously re-prompted every time). The Envoy opt-in prompt likewise honors a restored config instead of re-asking per project.
- **Venv probe hardened**: the synchronous venv-python probe ran on every `Start()` including every watchdog revive (recurring main-thread stall) and a probe *timeout* deleted the venv. It now probes once per session per venv path, a timeout falls back to system Python without touching the venv, timeout dropped 10s to 5s, and the probe no longer flashes a console window on Windows. Test-runner-suppressed parameter values no longer persist to config.json mid-run.
- **New shipped rule `worktree-td-safety`**: multi-step edits to externalized files belong in a git worktree; landing into the main tree wants TD closed (syncfile hot-reload), with a bidirectional drift check before porting. Ships to user projects via `_TEMPLATE_MAP_RULES`.
- **92 suites / 2,090+ tests**; 42 new/updated tests across the palette-scan, toxdrop, and settings-persistence suites. Known pre-existing failures on Windows (path-separator, shell-quoting, `chmod` no-op read-only-dir) are unrelated and tracked separately.

## v6.0.126

Two field-reported fixes (both from benjavides): the TDN save-time locked-content warning no longer fires for locked ops a nested externalization boundary already preserves (issue #53), and non-file-backed DATs no longer crash the externalization refresh sweep (issue #54). Fresh-install smoke-tested from the shipped `.tox`, which caught and fixed a residual issue-#54 sibling in the removal cleanup.

- **Locked-content warning respects nested externalization boundaries** (fixes [#53](https://github.com/dylanroscover/Embody/issues/53)): `_warnLockedNonDATs` scanned the whole subtree with `findChildren()`, so a locked TOP inside a nested TOX-strategy child COMP popped the "Locked Content Warning" on every save of the TDN parent -- even though the child's own `.tox` preserves that locked data fine, and the dialog's "switch this COMP to TOX" advice named the wrong COMP. The scan (extracted into a testable `_findLockedNonDATs`) now skips operators below any nested boundary the exporter itself skips -- a tox- or tdn-tagged child COMP (exported separately as a `tox_ref`/`tdn_ref` pointer) or an exclude-tagged subtree -- mirroring `_collectAllPaths`. A nested TDN child still raises its own warning when it exports itself, so nothing is silently lost. Docs updated (externalization guide + TDN spec).
- **Non-file-backed DATs no longer crash the refresh sweep** (fixes [#54](https://github.com/dylanroscover/Embody/issues/54)): `getExternalPath()` assumed every DAT has a `file` parameter, but selectDAT/mergeDAT and friends don't -- and a tracked path can come to resolve to one after a delete/rename swap, at which point `updateDirtyStates` killed the whole `Refresh()` with `td.tdAttributeError`. `getExternalPath` now returns `''` for non-file-backed DATs (so `checkOpsForContinuity` classifies the row as "replaced" and routes it through the existing recovery prompt instead of crashing), `setExternalPath` refuses them with a WARNING log, and the dirty-state sweep skips them without blanking the table's `rel_file_path` recovery pointer. Tag-time discovery already excluded them (`parName='file'` filter + `supported_dat_types`), so the guards close the stale-table-row gap, not a tagging gap.
- **Removal cleanup survives non-file-backed DATs** (found by the fresh-install smoke test of the fix above): `RemoveListerRow` cleared `syncfile`/`file` unconditionally on its DAT branch, so removing the tracking row for a type-swapped DAT raised a caught-but-logged `AttributeError` that aborted the color reset and parameter-tracker removal mid-cleanup. The par-clearing is now gated on the DAT actually having a `file` parameter.
- **92 suites / 2,090 tests** (+10: locked-scan boundary coverage -- direct child, locked DAT, nested tox/tdn/exclude, untagged nesting, tag-on-root -- non-file-backed DAT guards for `getExternalPath`/`setExternalPath`, and the `RemoveListerRow` completion regression). Fresh-install smoke test of the shipped `.tox` in a throwaway instance: status Enabled, zero script errors, all extensions live, Envoy bound, clean packaged defaults, and both fixes verified behaviorally against the packaged build (nested tox/tdn/exclude locked ops suppressed with direct/plain-nested controls still caught; the exact field crash path -- a tracked path resolving to a selectDAT during `Refresh()` -- completes and routes the row through the "replaced" recovery flow).

## v6.0.123

Editable keyboard shortcuts (issue #50): every Embody binding is now remappable, recordable, and disableable from a new Shortcuts parameter page.

- **Editable shortcuts** (fixes [#50](https://github.com/dylanroscover/Embody/issues/50)): the seven combo shortcuts (Manager, Update All, Update Current COMP, Refresh, Export Project/COMP TDN, Copy TDN) are now Str parameters on a new **Shortcuts** page -- type a combo like `ctrl+shift+o` (normalized and validated on entry) or leave one empty to disable it. The hardcoded `elif` chain in `keyboardin_callbacks.py` is replaced by a par-driven dispatch table built in the new `shortcuts` module DAT. `ctrl` and `cmd` are DISTINCT modifiers naming physical keys: macOS keyboards have both (matched exactly -- `ctrl+shift+o` and `cmd+shift+o` are different bindings, and `ctrl+cmd+k` is valid), PC keyboards have only Ctrl, so Mac-authored `cmd+...` bindings fold to Ctrl there at match/display time (values never rewritten -- they round-trip between platforms intact). Factory defaults use the platform's primary modifier (Cmd on macOS, Ctrl elsewhere). A combo may drive exactly ONE action: pressing an already-assigned combo while recording pops an explanatory dialog (via the auto-respondable `_messageBox`, so tests and the save window never freeze) and re-arms with a fresh timeout; typed duplicates revert with a warning. The tagger double-tap menu lists PHYSICAL keys and adapts per platform via a live `menuSource`: macOS offers Left Ctrl plus left/right Cmd (distinct keys, matched exactly; Apple keyboards have no right Ctrl); Windows/Linux offers left/right Ctrl (no Cmd key). A choice the other platform's keyboard lacks folds to its closest key at match time (Cmd->Ctrl on PC, right-Ctrl->left-Ctrl on Mac) -- the saved value is never rewritten, so it round-trips between platforms intact.
- **Shortcut recorder**: each binding has a **Record** pulse -- press the keys you want; held modifiers preview in the status bar and the first non-modifier keydown commits the combo (the industry-standard rule: no premature commit while modifiers are held, no indefinite wait once a real key lands). Esc cancels; an armed recorder auto-disarms after 10 seconds. While armed, Embody's own dispatch is suppressed so pressing a currently-bound combo records instead of firing.
- **TD built-in conflicts warn, never block**: assigning a combo TouchDesigner itself owns logs a WARNING and shows it in the status bar (Embody cannot suppress TD's own shortcuts -- both fire). The TD reserved list is parsed live from the effective `TouchShortcuts.txt` (factory table plus user overrides, honoring disabled rows) -- Embody cannot suppress TD built-ins, so the warning tells you both will fire.
- **Tagger double-tap is configurable**: a menu picks which modifier key double-taps to open the tagger (left/right Ctrl, Alt, or Shift) or turns it off; requiring left-Shift specifically in the combo shortcuts is dropped (generic `shift` now matches either side).
- **Bindings persist and surface everywhere**: the shortcut pars (and the Enable Keyboard Shortcuts toggle, previously unpersisted) are in `_PERSISTED_PARAMS`, so custom bindings survive Embody upgrades via `.embody/config.json`. Toolbar tooltips and the in-app help panel render the live bindings (tokens resolved at display time), and the six stale read-only shortcut display pars on the UI page are gone. Singleton detection now fingerprints on `Toxtag` instead of the removed `Addtagshort`. New `test_shortcuts` suite (48 tests: normalization, dispatch, reserved-list parsing, validation, the recorder state machine, parexec handlers, persistence whitelist). Test suite **92 suites / 2,080 tests**.

## v6.0.116

Two field-reported fixes -- removing a TDN externalization now sticks (issue #48), and Envoy no longer restart-loops on TD builds whose Textport stdout lacks `isatty()` -- plus version/minimum-build doc statements that rewrite themselves on save, a CONTRIBUTING guide, and five new specimen briefs.

- **Removing a TDN externalization sticks** (fixes [#48](https://github.com/dylanroscover/Embody/issues/48)): `RemoveTDNEntry` (the manager's X button for TDN rows) deleted the tracking row and the `.tdn` file but left the `tdn` tag on the COMP -- and the Update sweep that runs on every save re-externalizes any tagged-but-untracked COMP, so the row and file the user just removed came back on the next save. Removal now strips the operator's externalization tags, resets its color, and drops its parameter-tracker entry (mirroring `RemoveListerRow`), and tolerates paths with no live operator (Full Project rows track `/`). New `TestRemoveTDNEntry` regression class (5 tests), including a sweep-candidate check proving a removed COMP cannot be resurrected.
- **Envoy no longer restart-loops when `sys.stdout` lacks `isatty()`**: TouchDesigner replaces `sys.stdout` with a Textport catcher, and some builds (confirmed 2025.32460 on Windows, field report) ship one WITHOUT an `isatty()` method. uvicorn's default log formatter probes `sys.stdout.isatty()` when `use_colors` is unset, so `uvicorn.Config()` itself raised ("Unable to configure formatter 'default'") before the socket ever bound -- and the liveness watchdog restarted the dead worker forever, freezing TD every ~10-25 seconds. Envoy now passes `use_colors=False` (uvicorn's documented escape hatch; ANSI codes would be garbage in the Textport anyway). New `TestUvicornStdoutIsattyGuard` (2 tests: the source pin, plus `Config()` surviving an isatty-less stdout). A new troubleshooting section documents the symptom/fix, and the documented minimum build is corrected to **2025.32820** (builds that ship a catcher with `isatty()`).
- **Version and minimum-build statements now rewrite themselves on save**: `execute_src_ctrl.updateVersionDocs` (run by `onProjectPreSave`) rewrites the README version badge from `par.Version`, the TouchDesigner badge year, and the minimum-build lines in README.md, docs/index.md, and CONTRIBUTING.md from the running `app.build` -- the build we save with IS the support floor, and `app.build` replaces `project.saveBuild`, which pre-save still reports the PREVIOUS save's build. Substitutions are anchored per line (changelog/history mentions of older builds are never touched), and each file is guarded so a missing doc can never abort a save. New `test_version_sync` suite (6) fails on any drift between the badge, the three docs, and `par.Touchbuild`.
- **CONTRIBUTING.md**: a contributor guide for a repo where TouchDesigner writes many of the files -- contribution zones (open / TD-mediated / discuss-first), why TD-written files must not be reformatted, and how to run the test suite inside TD. Linked from the README.
- **Five new specimen briefs (06-10)**: Bridget Riley's *Current* (analytic op-art GLSL), the Vasulkas' Rutt-Etra scan processor (TOP-to-geometry displacement), Vera Molnar's *(Des)Ordres* (seeded Python builder writing instancing tables), a Calder mobile (hierarchical transforms + CHOP physics-feel + shadow rig), and Ryoji Ikeda's datamatics (DATs as visual material on a frame-exact clock). The briefs README now covers the set of ten.
- Housekeeping: the `text_rule_refresh_after_commit` template DAT is converted from a mis-typed `.py` to `.md` (it is a rule document, not Python), and the AGENTS.md template's rule 4 is realigned to the v6.0.111 deterministic-COMP-placement default (it still carried the old "current pane" guidance).
- **91 suites / 2,032 tests** (+13: issue-#48 removal regression, uvicorn isatty guard, version-doc sync). Full non-destructive run green (1,999 passed / 1 environment skip), and a fresh-install smoke test of the shipped `.tox` in a throwaway instance verified: status Enabled, zero script errors, all four extensions live, Envoy bound, both fixes present in the packaged build, and clean packaged preference defaults (`Filecleanup=keep`, `Toxdropexpr=ask`, `Envoyenable=0`).

## v6.0.113

TDN export survives broken widget clones (issue #46) and palette-clone blackboxing is restorability-gated -- verified end-to-end against the TauCeti preset manager (1,107 operators, 273 COMPs).

- **Broken clone expressions no longer abort TDN export** (fixes [#46](https://github.com/dylanroscover/Embody/issues/46)): truthiness on a `Par` object EVALUATES it, so `_isPaletteClone`'s `if not clone_par:` guard raised `tdError` on a widget clone expression referencing a missing master -- one line before the try written for exactly that -- and killed the whole export (including every subsequent checkpoint/save export of any tracked ancestor). Guards are now `is None` with the eval in its own try (`_isPaletteClone`, `_getCloneSourceDiffs`, plus the same landmine in `setupBuildParameters`); a raising clone expression exports as expression text, never evaluated.
- **Restorability gate for palette-clone blackboxing**: blackboxing omits children/custom pars on the promise the clone master refills them on rebuild. That promise requires cloning ON and a master that resolves LIVE -- a disabled or unresolvable clone (TauCeti's widgets: `fadetime` with 7 authored children + 124 custom pars, cloning off, defensive `hasattr` expression) was classified palette and would have rebuilt EMPTY. Classification (`_isPaletteClone`) and eligibility (`_cloneRestorable`) are now separate; unrestorable clones export in full.
- **Blackboxed palette clones actually restore on rebuild**: export used to strip `clone`/`enablecloning` from blackboxed entries, so a reconstructed shell had nothing to re-clone from and stayed permanently empty (a rebuilt lister: 0 of 31 children). The reference is now kept in the `.tdn` and applied on import BEFORE other parameter values (master content lands first, explicit exported values win -- the buttontype problem stays fixed), and only when the created op didn't auto-set its own resolving clone, so stale references in old files remain harmless.
- **Failed initial TDN export rolls the tag back**: `applyTagToOperator` no-ops while the tag is present, so a failed export left a dead end -- tagged but untracked, every retry silently doing nothing until the user stripped the tag by hand (the "remove the tdn tag and press ctrlctrl again" complaint in #46). The tag now rolls back on failure with an ERROR log naming the cause; re-tagging retries.
- **Suffix-style custom parameter groups round-trip faithfully**: export wrote the first COMPONENT's name for partial-arity groups (`Anchorx` for an XY group, `Tintr` for RGB -- TD reports style `RGBA` for both RGB and RGBA, `XYZW` for XY/XYZ/XYZW); import compensated by blindly stripping a trailing suffix letter, mangling legitimate base names (`Labelbgcolor` -> `Labelbgcolo` + r/g/b) and downgrading values-less RGBA groups to RGB (alpha silently dropped -- every TauCeti widget color par). Export now writes the group base name plus a true-arity `size` field (spec updated); import trusts the spec name (legacy component-named defs still detected narrowly) and picks the append variant from real arity.
- **Documented TD engine limit**: extra children on an ENABLED clone cannot be restored programmatically -- TD wipes non-master children whenever cloning is re-established, regardless of ordering (verified three ways); only the native `.toe`/`.tox` loader preserves that state. Such COMPs belong in TOX strategy or `tdn_exclude`. Captured where the importer handles clones and pinned by test.
- Verified against the real component from the issue: export 1.2s (was: crash), full ctrl-ctrl tag flow end-to-end (tag -> table row -> 377KB `.tdn`, all 14 broken clone expressions preserved as text), `fadetime` round-trips 7/7 children + 124/124 custom pars. **90 suites / 2,019 tests** (+14: broken-clone export and detection, restorable-blackbox round-trip, tag rollback with retry, RGBA/XY group fidelity).

## v6.0.111

Deterministic COMP placement for AI agents -- build where the user already works instead of a different network each run -- plus a geometryCOMP default-torus trap documented at the point every agent hits it. Skills and templates only; no source or test change.

- **Deterministic COMP placement via Embody-association.** The `/create-operator` "choose the parent network" step is rewritten so a new COMP lands in the SAME home every run instead of `/` one time and `/project1` the next. The default home is now the container that holds the `Embody` COMP (`op.Embody.parent().path`) -- the level the user chose by placing Embody there -- with a deliberately-opened content pane (`ui.panes.current.owner.path`) as a guarded override that is IGNORED when it sits at the bare root `/`. Container names are still discovered with `query_network`, never hardcoded to `/project1`. CLAUDE.md rules 3/5 (and the shipped `text_claude.md`) were realigned to this default so the always-loaded north-star no longer contradicts the skill.
- **geometryCOMP: delete the default torus.** A new `/create-operator` section (promoted from `/pop-networks` so it fires for SOP and imported geometry too, not just POP builds) documents that a fresh `geometryCOMP` ships with a `torus1` SOP whose RENDER flag is ON: the moment you add your own geometry, delete `torus1` (or turn off its render flag), or the Render TOP draws BOTH your geometry and a phantom torus. The trap is easy to miss because adding your own SOP auto-clears the torus's exclusive DISPLAY flag (viewer looks clean) while its non-exclusive RENDER flag keeps drawing -- so it bites only live `create_op` builds (TDN import already strips these auto-defaults).
- **Docs**: the never-before-released v6.0.109 features are now documented on the Envoy pages -- recovery hints in `docs/envoy/architecture.md` + `tools-reference.md`, and the `capture_top` Quality verdict in `tools-reference.md` + `index.md`. `/pop-networks` gains a cross-ref to the canonical create-operator torus rule. Suite unchanged at **90 suites / 2,005 tests**.

## v6.0.109

Two agent-ergonomics wins adapted from a competitor review: reactive recovery hints on failed tool calls, and a black/empty-frame verdict on `capture_top`.

- **Recovery hints on error envelopes**: when an Envoy tool returns an `error`, a `recovery_hints` list now rides back on the response -- each entry is `{cause, action, next_tools}`, matched by a small curated table (`_recovery_hints_for`) against the real error strings Envoy emits (path-not-found -> `query_network`/`find_children`, parameter-not-found -> `get_op`, wrong family, empty capture -> `get_op_performance`, thread conflict, timeout -> `get_project_performance`). Attached centrally in `_send_response` via `_attachRecoveryHints`: additive, never clobbers an existing block, never raises. It steers the agent's next step instead of a blind retry of the same failing call -- the reactive cousin of the `.claude` skills.
- **`capture_top` quality verdict**: every capture now computes a token-lean verdict from the raw float pixels (`_frame_quality`) -- luminance + alpha stats yielding `is_black` / `is_flat` / `fully_transparent` / `pass` / `fail_reasons`. It surfaces as a `Quality: OK|FAIL` line in the returned text, so the agent can tell an empty render from a real one WITHOUT reading the image -- enforcing the "never declare a visual task done on a black frame" rule as machine-checkable data. A uniform fill is advisory (`flat_frame`), not a failure; black and fully-transparent are failures.
- New `test_recovery_hints` suite (15): the match table against real Envoy error strings (including a live `get_op` failure) plus the additive/no-clobber/never-raise decorator behavior. 4 new `test_mcp_top_capture` quality-verdict tests (black -> FAIL, noise -> OK, solid-colour -> flat-but-pass, transparent -> FAIL). Verified live from the release `.tox` in a throwaway instance: both features shipped in the packaged v6.0.109 build and work end-to-end (black capture -> `Quality: FAIL ['black_frame']`, bad path -> `recovery_hints`), extensions up, Envoy running, no script errors. **90 suites / 2,005 tests.**

## v6.0.108

A one-click **Uninstall** for removing Embody from a project -- guarded by a confirmation dialog that spells out exactly what will be removed before anything is touched.

- **New Uninstall pulse** (Embody page, right after Disable). It computes the same NON-DESTRUCTIVE plan as `PreviewUninstall()`, then shows a `ui.messageBox` describing precisely what will happen -- how many Embody-generated items are **removed** (AI-assistant config like `CLAUDE.md` / `AGENTS.md` / `.claude` / `.cursor`, Embody's `.venv`, the `.embody/` state folder), which shared files are **modified** by stripping only Embody's block/key (`.gitignore`, `.gitattributes`, `.mcp.json`), which git config keys are **un-set** (the `.tdn` diff driver), and which items are **kept** (files you edited, an unrecorded venv). It runs the teardown ONLY when you confirm; Cancel -- or a suppressed save/test context -- is a no-op, so nothing is ever removed silently. Your externalized `.tox` / `.tdn` / `.py` files and the Embody COMP itself are never touched.
- **Wiring**: `UninstallHandler` (promoted) delegates to `embody_admin.uninstall_handler`, dispatched from the `Uninstall` pulse via `parexec`. The destructive `Uninstall(confirm=True)` API is unchanged underneath; the pulse just adds the interactive confirm gate on top.
- **Distinct from Disable**: Disable removes externalization *tags* and stops tracking (re-enable with Update); Uninstall reverses Embody's *install footprint* on disk (config, venv, git wiring, `.embody/` state). See the "Removing Embody" section in Getting Started.
- New `test_uninstall_handler` suite (5): the cancel path removes nothing, a suppressed save/test context defers instead of uninstalling, an empty root reports nothing-to-do, confirm removes exactly the footprint, and an edited generated file survives via the review bucket. The confirm-path tests seal `parexec` so `uninstall()` toggling Envoyenable off never stops the live server. A `test_smoke_release` assertion also confirms the release `.tox` ships the Uninstall pulse + handler (verified live: a fresh v6.0.108 build loaded from the release `.tox` in a throwaway instance -- extensions up, Envoy running, no script errors, Uninstall param/handler present). **89 suites / 1,986 tests.**

## v6.0.106

The ext diet: EnvoyExt and EmbodyExt split into thin facades plus focused module DATs -- ~5,900 lines relocated with zero functional change, byte-identical MCP tool schemas, and three latent bugs fixed along the way.

- **EnvoyExt: 9,221 -> 5,110 lines (-45%)** across five module DATs: `envoy_layout` (network lint + dock-hug + auto-position geometry), `envoy_viz` (the entire Embot + camera-follow subsystem, 29 methods), `envoy_ops` (19 mutating tool handlers), `envoy_read` (26 read/introspection handlers), `envoy_setup` (MCP/registry/git config, 21 methods). Every moved method keeps a delegating stub, so the public API, dispatch table, undo wrapping, and all monkeypatch seams are unchanged.
- **EmbodyExt: 10,217 -> 8,817 lines** across three module DATs: `embody_launch` (AI-client launcher), `embody_git` (AI-config/template/manifest generation + git status + InitEnvoy/InitGit/Reset), `embody_admin` (uninstall + settings persistence). The save/continuity/restoration engine core stays on the facade deliberately -- it is one interwoven subsystem, and splitting it would add risk without adding clarity.
- **Thread-safety rules enforced mechanically**: worker-executed code (the docs lookup, the env/venv installer core, the git-status worker) stays on the facade because `mod.*` is a TD object and off-limits off the main thread; the git-status worker now captures its parser as a plain module function resolved on the main thread. The env cluster's extraction was evaluated and correctly refused after thread classification.
- **Three real bugs fixed**: `_checkMCPUpdate` called `run()` from its worker thread, so the MCP-update notice never logged (now an attribute-publish + bounded main-thread poll, with regression tests); a never-raises contract at the dispatch chokepoint was restored; the operator auto-position scan and overlap warning no longer treat annotations as obstacles.
- **Verification discipline**: each package passed an adversarial review panel (AST-verified byte fidelity including every generated-template string literal, worker-closure audits, and a test-contract lens proving no suite went vacuous) plus live gates in the running TD session. Full suite green (the lone reported failure is the test-runner's own Status override, verified 33/33 via the direct runner).
- **Module DATs ship in lockstep with the .toe** so a fresh clone can never open a .toe whose extensions reference not-yet-restored module DATs (settings restore fires at frame 5; DAT restoration at frame 50).
- test_server_lifecycle grows to 24 tests (MCP-update marshal coverage). **88 suites / 1,979 tests.**
- TDNExt's split (shared serialization/fileio/refs modules, then export/import/clipboard) is mapped and deferred to a follow-up -- see dev/embody/plan-ext-diet.md and the boundary map for the full extraction plan.

## v6.0.104

Docked operators now hug their hosts mechanically -- dock placement moved from written guidance into the Envoy tool layer -- plus a docs transparency pass and the first Specimen brief set.

- **Docked companions auto-hug their host.** `create_op` and `copy_op` now snap every docked companion an operator spawns (GLSL pixel/compute/info DATs, callback DATs) into a tight row 30 units below the host, centered, slots dock-width+20 apart (`docks_placed` in the result). `set_op_position` carries a host's docks along when you move it (`docks_moved`), so repositioning a GLSL TOP no longer strands its shader DATs -- the #1 way scattered docks actually happened. The auto-positioner also reserves the dock-row footprint, so a new host lands where its companions fit too.
- **`execute_python` auto-fix + tighter lint.** Docks of newly-created ops left scattered by a script are auto-hugged below their host before the layout lint runs (a `LAYOUT WARNING` reports the fix); deliberate near-host placements are left alone. The scattered-dock lint threshold tightened from 500 to 350 units -- the old threshold let visibly-stranded docks (~400u away) pass silently.
- **`get_network_layout` reports `dockedTo`.** Docked companions carry their host's name, so the layout Verify step can check "every dock hugs its host" mechanically instead of by eyeball.
- **Skills/rules/templates synced**: the create-operator skill gains an explicit docked-companions step and Verify item, network-layout.md documents the tool-layer enforcement (manual formula now scoped to `execute_python` builds), mcp-tools-reference rows updated -- all three shipped templates regenerated and normalized to UTF-8/LF/no-BOM.
- **Docs transparency pass**: honest build-time expectations (small changes in seconds, complete networks are 5-20 minute autonomous builds; multi-session parallelism as the real velocity story) across the landing page, manifesto, and quickstart; **Auto-Externalize New Ops** and **Tool Permissions** parameter documentation; Launch AI Client as the primary quickstart path; button-hover contrast and footer-spacing CSS fixes.
- **Specimen briefs** land in `dev/specimen-briefs/` -- the authoring contract plus five briefs (point-line-plane, overture, digital-harmony, lumia, radiolaria) for the Specimen Collection gallery.
- `.gitignore` now covers `.embody/` config dirs at any depth (machine-local catalogs/manifests no longer show as untracked).
- `test_layout_lint` grows 11 -> 16 tests (hug formula, dock-follow, dock-self-move, `execute_python` auto-hug, 350 boundary). **88 suites / 1,977 tests.**

## v6.0.103

A "How should the AI ask permission?" step in the setup wizard so you choose your Claude Code tool-permission posture -- plus the wizard itself is now externalized to TDN.

- **New wizard step: tool permissions.** When you turn on Claude Code in the setup wizard (Auto *or* Advanced mode), a new step lets you choose how much Embody pre-approves Envoy MCP tool calls in `.claude/settings.local.json`, so Claude Code stops asking on every tool use: **Don't ask** (recommended -- auto-approves all Envoy tools via the `mcp__envoy` wildcard, so new tools are covered too), **Ask for some** (read-only/query tools only; anything that creates/edits/deletes still prompts), **Ask for all** (pre-approve nothing), or **Leave settings alone** (never create or modify the file). The choice persists on a new **Tool Permissions** (`Toolpermissions`) parameter on the Envoy page. The step shows for Claude Code only, since `settings.local.json` is Claude-specific.
- **Captured TOPs no longer prompt to read.** Every written posture also whitelists the operating-system temp directory (`tempfile.gettempdir()`, macOS and Windows) in `additionalDirectories`, so a PNG saved there by `capture_top` can be read back without a per-file permission prompt.
- **Non-destructive settings writer.** The `settings.local.json` writer now merges into an existing file, preserving all your other keys (hooks, model, other allow patterns), only rewrites when the posture actually changes (no startup churn), and logs whether it created or updated the file. The shipped template shrank to a non-Envoy baseline; the Envoy allow entries are generated per posture in code.
- **Setup wizard externalized to TDN.** The `wizard` COMP is now a first-class externalized artifact -- `wizard.tdn` (structure) plus `wizard/logic.py` and `wizard/clicks.py` (its hand-authored step machine and click router) -- diffable in git like Embody's other UI COMPs, and (as an Embody descendant) safely excluded from TDN reconstruction/stripping.
- **The permissions step fits the window.** Its four options get a vertical scrollbar (with a peek of the fourth as a scroll cue) so the step never pushes the Back/Next footer off-screen.
- New `test_tool_permissions` suite (10) covers posture -> allow-list mapping, `leave` = no write, merge-preserves-keys, and idempotency; `test_setup_wizard` gains 3 for the posture plumbing. **88 suites / 1,972 tests.**

## v6.0.99

Setup-wizard layout polish and a size-aware network-spacing rule.

- **Wizard option buttons: consistent vertical centering and left alignment.** Every option button's title and subtitle now share one left edge and sit centered in the button on every screen (button text offsets normalized). Fixes the assistant/client/footprint buttons reading top-heavy while the mode buttons looked centered.
- **"Review what Embody will add" hint fixed.** The description text was `hmode='fill'` -- spanning the full window with no right margin -- so long copy ran off the right edge and clipped, and it reserved a fixed 88px block that left a dead gap above the options. It is now constrained to the 452px content column (wraps cleanly inside the panel, no clipping) with a height that fits the wrapped text.
- **De-overlapped wizard button tiles in the network editor.** The panel-widget button COMPs were stacked 100px apart while their tiles are 134px tall, so they overlapped in the editor (cosmetic -- panel layout is align-driven -- but messy). Tiles are now spaced by actual node height, zero overlap.
- **New layout rule: spacing is `size + gap`, both axes, never a fixed step.** `network-layout.md` (and its shipped template) now specify computing every offset from actual `nodeWidth`/`nodeHeight`, with the vertical formula `step = ceil((maxNodeHeight + gap) / 200) * 200`, and explicitly cover panel-COMP widget tiles (which the LAYOUT WARNING lint does not police). This is the rule that prevents the tile-overlap class of bug above.

## v6.0.92

Setup-wizard text alignment (for real this time) and the last main-thread freeze removed from Envoy startup.

- **Wizard option titles and subtitles are ink-aligned on every platform.** Two stacked causes: the title used the multiline field renderer while the subtitle used the string label renderer (identical insets on macOS -- which is why it looked fixed there -- but divergent on Windows), and a size-14 first glyph carries ~2px more left bearing than a size-11 one. All 28 title/subtitle Text COMPs now share the multiline renderer (which also un-breaks subtitle word wrap, silently dead under string type), and subtitles carry a +2px offset compensation. Verified by pixel measurement: title and subtitle ink both start at column 19 on the Claude Code option.
- **No more multi-second freeze after dependency install.** The pip/venv install was already threaded, but the post-install import gate (mcp + pydantic + starlette + uvicorn, cold pyc compile) ran on TD's main thread -- field logs showed the frame counter pinned for ~6s. The gate now runs on a worker thread in both the first-install and every-open paths, with Envoy Status showing "Preparing Python environment..." during the warm-up and a once-per-TD-session flag so saves and server restarts skip it entirely. Verified live: server restart ran the new flow and recovered with the gate flag set.
- Setup-environment suite adapted and extended (thread-callable import gate, idempotent path wiring, session flag): 1,959 tests total.

## v6.0.91

Rules diet: the always-loaded rule files shrink ~60 percent by relocating reference depth into four new on-demand skills -- every hard law stays inline, every moved section leaves a MUST-load trigger behind.

- **Four new shipped skills (13 total):** `/movie-export` (the Realtime trap, zero-drop verification, deterministic export, async-reader staleness -- loaded only when actually rendering), `/parameter-design` (pages/styles/ranges/help-text catalog), `/td-recovery` (bridge internals + manual recovery runbooks), `/multi-session-etiquette` (advisory contract, claim leases, gates).
- **Thinned always-loaded rules:** performance.md 21.0k -> 9.7k bytes, td-python.md 20.1k -> 15.2k, parameters.md 5.3k -> 1.5k, td-connectivity.md 6.1k -> 1.3k, multi-session.md 3.1k -> 0.9k -- roughly 10k tokens reclaimed per session, and per-project relevance: a project that never exports movies never loads the movie-export saga. Threading/cook depth moved into `/td-api-reference`, which was already mandatory before writing TD Python, so enforcement is unchanged by construction.
- **Relocation verified mechanically:** all 263 substantive lines from the original rules confirmed present in either the thinned rule or its destination skill (zero content lost).
- **Peers hint:** the FIRST multi-session advisory served to each session now carries a `_hint` pointing at `/multi-session-etiquette`, so coordination guidance arrives exactly when a second session appears.

## v6.0.90

Token and latency quick wins from the efficiency audit, plus the output-first visual convention.

- **get_op on a diet.** Returns NON-DEFAULT parameters only by default (include_defaults=True restores everything; parameters_omitted reports the filtered count). A parameter-heavy COMP read drops from ~13.7k chars to a few hundred -- the single largest per-call token sink, and it aligns with the non-default behavior users compared us against.
- **Compact read shapes.** query_network drops the redundant name field (derivable from path); get_network_layout drops name/family/node centers (centers = nodeX + nodeWidth/2, the math the layout rules already teach) and caps annotation text at 160 chars; get_parameter gains details=True with a lean default (keeps value/mode/expressions/menuNames).
- **Docstring diet: -14.1k chars from tools/list.** The 10 heaviest tool schemas trimmed from 20,902 to 6,754 chars -- eager-loading clients (Gemini CLI, Codex) stop paying ~3.5k tokens of teaching prose per session; the teaching lives in the mcp-tools-reference skill.
- **~5ms off every call.** The worker's response delivery is now event-driven (blocking queue get) instead of a 10ms poll.
- **Bridge tools/list no longer double-appends meta-tools** on cache hits.
- **Upgrade tracebacks silenced.** The Envoy watchdog and TDN clipboard-watch reschedulers now null-guard their deferred callbacks, so replacing/upgrading the Embody COMP no longer prints AttributeError tracebacks from orphaned run() loops.
- **Output-first visual convention (new).** The visual-aesthetics skill, generated CLAUDE/AGENTS guidance, and dev docs now direct agents to create an Out TOP named out1 FIRST, turn on its display flag, and keep the working chain wired into it -- the user watches the piece take shape live in the network backdrop while the agent works.
- **AGENTS.md parity.** The non-Claude guidance gains the batch-operations rule and group-level (not per-op) verify cadence, matching CLAUDE.md.

## v6.0.89

Launch AI Client fixes: Windows parity and visible errors.

- **Windows terminal launches now guard for a missing CLI.** Launching Claude Code / Codex / Gemini on Windows generated a raw `cmd /K "gemini"` -- an uninstalled CLI produced cmd's cryptic "not recognized" error instead of guidance. Windows now gets a generated `.bat` twin of the macOS `.command` script: a `where` guard that prints the same install instructions and keeps the console open. Pure builder, unit-tested (goto-flow so hints with parentheses cannot corrupt cmd block parsing; CRLF).
- **"VS Code" launches again.** The Aiclient menu and wizard offer VS Code, but the launch table only wired `copilot` to the VS Code launcher -- selecting VS Code logged "No launcher" and did nothing on any platform. Added the missing `vscode` mapping.
- **Launch failures now show a dialog.** Every failure path of the Launch AI Client pulse (no launcher wired, editor not installed, terminal failed, unexpected error) raises a message box with the install hint, in addition to the log line -- the message also now names the selected client instead of the parameter ("VS Code", not "AI Client"). The Windows missing-CLI case keeps its instructions in the opened terminal (no double dialog).
- **Launcher suite grows to 29 tests** (Windows batch builder content, vscode mapping, dialog consumption, label regression).

## v6.0.88

Setup-wizard hotfix on top of the v6.0.87 feature release (below): the wizard's buttons were dead in every user project.

- **Setup wizard clicks work in user projects again.** The wizard's Panel Execute DAT watched 16 ABSOLUTE op paths (`/embody/Embody/wizard/...`) that only resolve in the dev project; in a user project the tox lands at `/Embody`, the watcher resolved to nothing, and Next/Back silently did nothing (no errors -- the Auto option still looked selected because it is a native radio latch). Broken since the wizard shipped in v6.0.74; masked in dev and never exercised by the smoke harness, caught by the first real Windows click-through. Now uses relative patterns, verified end-to-end with simulated real clicks, and guarded by a new regression test that forbids absolute paths in any Embody panel watcher.

## v6.0.87

Envoy grows to 53 MCP tools with undoable edits, official TD docs lookup, numeric TOP sampling, tighter parameter/script guardrails, explicit transport hardening, and new POP-building guidance.

- **Undoable MCP mutations.** Every mutating Envoy operation in `_UNDOABLE_OPS` now runs inside a TD undo block -- adapted from Derivative's TDMCP with permission -- so one `batch_operations` request collapses to one Ctrl+Z step instead of a pile of per-op edits.
- **Official TD docs from Envoy.** New `get_docs` tool brings the live TD tool surface to **53 MCP tools** and looks up official TouchDesigner docs by preferring the version-exact offline help mirror under `<Samples>/Learn/offlineHelp`, then falling back to docs.derivative.ca's MediaWiki API. Section drill-down uses `sections_available`, and the HTML/API fetch work happens on the MCP worker thread so TD's frame loop stays clear.
- **`capture_top(sample_grid=...)`.** `capture_top` can now return a clamped 2..32 NxN RGBA sample grid instead of an image, with row 0 at the top and full-resolution per-channel min/max/mean. It is token-cheap, machine-assertable, preserves HDR values above 1.0 that image capture clips, and sanitizes NaN/Inf values.
- **Parameter search mode.** `get_parameter` now has glob search over parameter names, evaluated values, expressions, and bind expressions across a bounded subtree (`search`, `search_in`, `depth`, `max_results`), so searches like `search="*/project1/*", search_in="expr"` expose absolute-path expressions.
- **Safer parameter writes.** `set_parameter` now rejects invalid Menu values with `menuNames` / `menuLabels` instead of accepting TD's silent index-0 coercion, includes a label-to-name hint when the caller sends a label, and auto-grows sequence-block parameters such as `const5name` to 6 blocks.
- **`execute_python` rollback contract.** A script exception now destroys operators created by that call and reports the rollback count, while mutations to pre-existing operators remain in place; the whole call is still Ctrl+Z-able, and the generated TD UI rule now states that true contract.
- **Transport security pinned.** Envoy now passes explicit FastMCP `TransportSecuritySettings` for Host/Origin validation and DNS-rebinding/CSRF defense instead of relying on SDK defaults; the security docs now say the localhost bind alone is not the defense.
- **New `pop-networks` skill.** The ninth shipped skill adds POP-family builder guidance adapted from Derivative's TDMCPSkills `td-pop-family` with permission: POPs vs SOPs, the `geometryCOMP` ritual, particle lifecycle, `glslPOP` discipline, trap list, and Embody's performance/layout/naming/verification gates. The template DAT, `_TEMPLATE_MAP_SKILLS`, release sync table, prerequisite row, and generated Claude list are wired.
- **New drift and tool-guard tests.** Added `test_envoy_tool_guards` (29 tests: undo wiring including live Ctrl+Z proof, menu/sequence guards, parameter search, `execute_python` rollback, `get_docs` parsing, and `sample_grid`) and `test_template_sync` (5 tests: template map/disk/release-table sync plus orphan allowlist), bringing the source inventory to **87 test suites / 1,940 test methods**.
- **Stale Envoy tests repaired.** Six stale tests across `test_envoy_thread_comm`, `test_envoy_bridge`, and `test_server_lifecycle` now match the current per-session log-cursor and `_process_is_real_td` contracts; queue-based thread-comm tests use private queues so they no longer inject fake requests into the live MCP queue or drain real sessions' pending calls.
- **Tool reference sync.** The `mcp-tools-reference` skill/template, docs/envoy tools pages, docs index, and README counts are synced to 53 tools, including `get_docs` and `capture_top`'s `sample_grid` mode.

## v6.0.83

Multi-session Envoy coordination, 52 MCP tools, and a TDN stability pass. This release makes parallel AI/client work more visible and safer, tightens destructive-operation behavior, hardens several TDN edge cases, refreshes the generated agent guidance, and regenerates the 6.0.83 release artifacts.

### Envoy multi-session coordination

- **52 MCP tools.** Envoy now exposes `claim_scope` and `release_scope` alongside the existing `get_sessions` view, bringing the live TD tool surface to 52 tools plus the bridge meta-tools.
- **Live-session awareness.** `get_sessions` now reports recent scopes and claims so agents can see which peers are active and what part of the project they are working on.
- **Peer advisories on tool responses.** MCP responses can include `_peers` metadata when another live session is active nearby, giving agents enough context to coordinate before editing the same network area.
- **Destructive-operation gates.** `delete_op`, `import_network(clear_first=True)`, `run_tests`, and `batch_operations` now refuse risky work when another recent session owns or touched the relevant scope unless the caller passes `override=True`.
- **Per-session log cursors.** Recent log piggybacking is tracked per session, so one client no longer drains another client's warning/error feed.

### TDN stability

- **Import validates before clearing.** Malformed TDN is rejected before `import_network(clear_first=True)` clears an existing COMP.
- **DAT editability capture is non-mutating.** Capturing `isEditable` no longer changes the live DAT state while exporting.
- **Flag defaults round-trip more cleanly.** Object COMP render/display defaults and noise terrain default flags no longer produce avoidable TDN churn.
- **Stale cleanup is tracking-aware.** Cleanup only removes tracked `.tdn` files; ad-hoc untagged exports no longer enroll themselves into Embody tracking.
- **Orphan shell recovery remains intact.** `_tdn_rel_path` recovery for orphan shells is preserved across the export/import path.
- **Malformed templates degrade gracefully.** Bad generated-template content is handled without cascading into broader TDN failure.

### Setup and generated guidance

- **Setup Wizard polish.** The AI-client picker no longer forces a scrollbar now that the option list is shorter, and wizard copy has more right-side padding so text does not crowd the window edge.
- **AI-client menu reflects current support.** The standalone VS Code client token has been removed; GitHub Copilot remains supported through VS Code.
- **Agent guidance updated.** The generated multi-session rule/template and default MCP allowlist now include `claim_scope` and `release_scope`.

### Tests and release artifacts

- **New regression coverage.** Added `test_tdn_stability_hardening` and expanded `test_envoy_sessions` for multi-session coordination and destructive-operation behavior.
- **Current test source inventory.** The repo now contains **85 test suites / 1,906 test methods**.
- **Release artifacts refreshed.** The development `.toe`, generated `.tdn` files, externalization table, and shipped release artifact were regenerated for **6.0.83**.

## v6.0.69

A new **Dropped .tox Expression** control, plus a **data-safety hardening** of the test harness driven by a real incident: destructive whole-project test suites can no longer run as part of a normal test run, so a full `RunTests()` can never mutate your live project.

### Embody core

- **Dropped .tox Expression (`Toxdropexpr`).** New menu on the Embody page controlling how the continuity sweep treats the default expression TouchDesigner auto-writes into a COMP's External .tox when a `.tox` is dragged in (`me.parent().fileFolder + '/' + ...`). `Ask` (default) prompts on detection with a list that truncates past a cap (so the dialog buttons stay reachable) and four choices -- **Clean**, **Ignore**, **Always Clean**, **Always Ignore**; the two "Always" buttons persist the choice into the parameter so you are not re-prompted. Embody's own descendants are always cleaned. The prompt now routes through the test-seedable `_messageBox` instead of a raw `ui.messageBox`.
- **Removed self-heal param bloat.** `_ensureAutosaveParams` (EmbodyExt) and `_ensureVizParams` (EnvoyExt) recreated their own custom params on every init -- unnecessary, since params persist in the `.toe`. Both deleted; the params remain baked into the build.
- **Continuity sweep never touches Embody's own subtree.** `checkOpsForContinuity` now hard-skips rows under Embody's own path, closing a gap where a transiently-missing Embody COMP could be deleted or re-externalized during strip/restore thrashing.
- **TDN `export` mode announces itself.** `ReconstructTDNComps` now logs its export-mode action (additive recovery only, existing COMPs kept), matching the `off` and `full` branches.
- **Fixed the shipped `CLAUDE.md` template's accuracy.** `templates/text_claude.md` (which generates a user project's `CLAUDE.md` / `ENVOY.md`) still wrongly called `.tox`/`.toe` "text files" and described the deployed `.claude/settings.local.json` as read-only-only -- it had drifted behind the v6.0.66 accuracy sweep, so regeneration reverted that sweep. Corrected to match: `.tox`/`.toe` are opaque binary (`.tdn` is the text format), and Embody's `settings.local.json` pre-allows the write tools it actually deploys (`create_op`, `set_parameter`, `execute_python`, `import_network`, ...), written only if the file is missing and never overwritten.

### Test-harness data safety

- **Destructive whole-project suites are segregated.** A test suite that calls `Disable` / `ExternalizeProject` / `Reset` on `ext.root` (the ENTIRE live project) now sets `DESTRUCTIVE = True` and is EXCLUDED from every normal run (`RunTests` / `RunTestsSync` / `RunTestsDeferred*`). Such suites run ONLY via the opt-in, save-gated `RunDestructiveTests(confirm_saved=True)`, which refuses on an unsaved project so a recoverable `.toe` always exists. A plain full run can no longer mutate the live project. New dev rule `rules/destructive-tests.md` documents the convention and the incident it prevents.
- **`Filecleanup` cannot get stuck at `delete`.** The test runner's suppress/restore of `Filecleanup` is now re-entrancy-guarded, so an interrupted or timed-out batch cannot leave it stuck at `delete` -- a stuck value turns any file operation into a silent unlink.

### Tests

- New `test_toxdrop_expr` suite (10 tests: the menu, all four dialog buttons, silent clean/ignore, Embody-descendant always-clean, and dialog-list truncation). `test_dialog_suppression` hardened to diff the log buffer by entry `id` rather than a positional slice on a bounded `deque(maxlen=200)`. Test suite **76 suites / 1,761 tests**; the normal `RunTests()` (1,729 tests, destructive suite excluded) is green with 1 conditional skip, and the 32-test destructive `test_custom_parameters` runs separately via `RunDestructiveTests`.

## v6.0.66

A one-click **Launch AI Client** button on Embody's Envoy page: pick your assistant in the `Aiclient` menu, press the button, and Embody opens it at the project root -- editors (VS Code, Cursor, Windsurf; Copilot -> VS Code) open the folder as a workspace, terminal CLIs (Claude Code, Codex, Gemini) open in a new terminal. Built to survive the real cross-platform traps and hardened by a 10-agent codex cross-platform review.

### Embody core

- **Launch AI Client button (`Launchaiclient`).** New Pulse parameter beside `Aiclient` that opens the selected client at `_findProjectRoot()` (honoring `Aiprojectroot`). One `_AICLIENT_LAUNCH` table drives it; two helpers (`_launchEditor`, `_launchTerminal`) hold all `sys.platform` branching. Editors resolve the REAL app/exe -- macOS via LaunchServices (`/usr/bin/open -b <bundle-id>`, then `-a "<Name>"`, then the app's own bundled CLI), Windows via the real `Code.exe`/`Cursor.exe`/`Windsurf.exe` from known install dirs -- never a hijackable `code` PATH shim (Cursor installs its own). CLIs run in a real terminal so its login shell rebuilds PATH, which defeats the Dock-truncated-PATH problem where a CLI in `~/.local/bin` is invisible to a Dock-launched TD (macOS writes a `.embody/launch_<cli>.command` handed to `open`; Windows uses `cmd /K`). A missing tool prints a verified per-tool install hint instead of a false "launched".
- **Fixes the "dock icon bounces, then closes" launch bug.** TouchDesigner sets `ELECTRON_RUN_AS_NODE=1` (plus `LD_LIBRARY_PATH`/`DYLD_*`/`PYTHON*` into its own bundle), and macOS `open` forwards the caller's environment, so a freshly launched Electron editor (Cursor/VS Code/Windsurf) ran headless-as-Node and quit instantly. A new `_launchEnv()` strips those vars for every launch; verified live (Cursor stayed open).
- **Gemini config generation.** Selecting Gemini writes a thin `GEMINI.md` that imports the always-written `AGENTS.md` via Gemini's `@AGENTS.md` syntax -- no duplication. The `Aiclient` menu gains `codex`, `gemini`, `vscode` (the five existing tokens preserved verbatim so persisted settings never break).
- **`.gitignore`:** other AI clients' generated configs (`.cursor/`, `.windsurf/`, `.github/copilot-instructions.md`, `.github/instructions/`, `GEMINI.md`) are now ignored -- this repo's own client is Claude Code, whose `.claude/`/`CLAUDE.md`/`AGENTS.md` stay tracked.

### Cross-platform review

- A **10-agent codex cross-platform panel** (5 initial lenses + 3 verification, plus an orchestrator self-audit) drove the launcher to correctness: whole-body crash-safety in `LaunchAIClient`; helpers return `bool` so success logs only on a real launch; the Windows editor shim resolves via `shutil.which` then runs the `.cmd` through cmd's doubled-quote form (spaces + `&`/metachar safe); `/usr/bin/open` so launches work even if TD's PATH lacks `/usr/bin`; `${SHELL:-/bin/zsh}` used consistently in the generated `.command`. macOS is verified live; Windows is review-verified (bench-testing pending).

### Tests

- New `test_launch_aiclient` suite (17 tests: launch-table shape, CLI resolution, `.command` generation + quote-escaping, env sanitization, editor graceful failure) plus 7 new `test_claude_config` tests (Gemini `GEMINI.md` + `_clientFilesMissing`). Test suite **75 suites / 1,751 tests**, all green.

## v6.0.62

A performance-rule expansion shipped in the `.tox`: a complete **Movie Export / Offline Rendering** playbook so an AI agent recording a movie never ships a juddered file. No core-code change -- this is agent guidance, delivered to user projects through the `performance.md` rule template baked into the build.

### Agent rules

- **Zero-dropped-frame movie export.** `rules/performance.md` (and its shipped template `text_rule_performance.md`) gains a "Movie Export / Offline Rendering" section built around the #1 cause of juddered exports: the **Realtime flag** (`project.realTime`, ON by default) silently **replicating** any frame TD can't cook within the `cookRate` budget, so a recording ends up the right LENGTH but full of duplicate frames. The rule now mandates capturing the prior flag and going non-realtime before a render; routing every exit path (last frame written, a force-cook exception, a drop-abort, and user cancel) through one `_finish(prior)` helper that restores the flag and stops recording, since there is no `try/finally` spanning the async `run(delayFrames=...)` driver; monitoring the Movie File Out Info CHOP `total_frames_dropped` **during** the render and aborting on the first drop instead of discovering it after minutes of GPU time; and proving the result with **two separate** checks -- length (`total_frames_written`, `ffprobe -count_frames`) and uniqueness (`total_frames_dropped == 0` plus `ffmpeg mpdecimate` / `framemd5` duplicate detection), because a juddered file still passes the length check. Includes a deterministic per-frame export recipe (`type='stopframemovie'`, `addframe.pulse()` stepped one frame per `run(delayFrames=1)`, force-cook-and-confirm before each pulse), a "let the encoder drain before verifying" caveat, and a correction that `performLongOperation` is not a documented API.

### Tests

- Test suite unchanged at **74 suites / 1,727 tests** -- this release is agent guidance shipped in the `.tox`, with no Python code change.

## v6.0.61

An Embot polish pass aimed squarely at the spawn-time frame drops, plus more character. The mascot now assembles **off-view and swoops in whole** instead of stuttering together in the net you're watching, and he picks up an occasional happy squint and a cleaner shrug.

### Embody core

- **Embot spawns without the frame-drop sag.** Copying an annotateCOMP into the network you are *viewing* costs ~280ms (the in-viewport annotation-layer redraw); copying it *outside* the viewport costs ~100ms (measured). So on an on-screen spawn Embot now assembles at an off-view staging point just past the viewport edge and **swoops in once whole** -- each part's copy renders off-screen, so the fps sag is far shallower and you see a clean entrance instead of a stuttering build. Dives still snap in place (already cheap, off-screen). The fix was chased through `copyOPs`, `ui.pasteOPs`, and a redraw-suppressed block copy -- all of which crash TD on repeat into a *displayed* net (one annotate at a time is the only stable primitive) -- before landing on off-view staging.
- **Paced, ordered assembly.** The on-screen spread copies one part every `_VIZ_ASSEMBLE_INTERVAL` frames (32) in a body -> head -> speech -> limbs -> eyes order, so the per-part redraw hitches stay isolated instead of fusing into a freeze, and he reads as "building himself" rather than sitting half-built.
- **A happy squint.** Every ~9-17s Embot briefly flattens and spreads his eyes into a content `^_^` (separate from the ~2-5s blink). His eyes are a touch bigger now (12x13) so the squint has height to flatten *from* -- TD clamps an annotation node to a 10px floor, so the eyes must start tall enough to visibly squint (the same floor that made a scale-Y blink impossible).
- **Shrug, not stretch.** The arms-up gesture used to lift the arms by *scaling their height* (a weird stretch); it now just raises them straight up.

### Tests

- Test suite unchanged at **74 suites / 1,727 tests** -- this is runtime character/camera behavior in `EnvoyExt`, with no new Python unit coverage.

## v6.0.57

A live-build-visualization split plus a major embody.tools Collection upgrade. In TouchDesigner, the opt-in build visualization (shipped in v6.0.54) is now two independent toggles -- the **Embot** character and the **Envoy Follow** camera -- and self-heals so it survives a restart. On the web, specimens gain **multiple categories**, **private drafts**, a **license picker**, and a meaningfully better TDN editor/profile.

### Embody core

- **The build visualization splits into Embot (character) + Envoy Follow (camera), each separately toggleable.** v6.0.54 bundled the mascot and the camera under one `Envoyfollow` switch; they are now `Embotenable` (the little builder who stands on each operator and narrates what he just did) and `Envoyfollow` (the network-editor camera that pans to the active op). The camera frames the *operator* now, so it follows Envoy's work whether or not the character is shown.
- **The toggles self-heal on every init.** They were added live in a session and vanished on the next restart; a new `_ensureVizParams()` recreates them if missing (idempotent, bakes into the `.toe` on save), so the feature is always controllable.
- **Per-frame bot assembly restored.** Embot is copied from his template one part per frame -- the version that ran stably for hours -- replacing a single block `copyOPs` that was implicated in repeated TD crashes.
- **Past-tense narration.** Embot describes the node he just finished and is standing on ("seeded a noise texture"), keyed on `OPType`, with coverage expanded across TOP / CHOP / SOP / POP / MAT / COMP / DAT.
- **Follow no longer freezes on TD's auto-frame.** The user-takeover detector now yields only on a real network change (you click into another COMP); a transient pan/zoom from TD auto-framing a freshly-spawned node used to stall the follow for ~6s while Embot raced off.

### embody.tools

- **Specimens can belong to several categories (up to 3).** A new `specimen_categories` join table backs ANY-match facet filtering and the category facet list; `specimens.category` stays the primary (single-slot display + thumbnail motif + back-compat). Cards show a `+N` badge; the detail breadcrumb links each category. (D1 migration `0010`, backfilled.)
- **Private drafts + a publish toggle.** New uploads default to **private** -- yours to preview and refine -- and you publish (or unpublish) from the specimen page or delete from your profile. Owner-scoped reads let you see your own drafts; everyone else sees only public. Your profile splits specimens into public/private groups with a persisted list/gallery view toggle and inline edit / arm-to-delete controls.
- **License is a real picker.** A fixed SPDX-style vocabulary (Creative Commons family + common code licenses + all-rights-reserved) replaces the free-text field on submit and edit; off-list values coerce to the default, and a legacy value survives an edit. The detail page shows the actual license.
- **A better TDN editor + viewer.** The editor gains a search match counter with prev/next/clear, a go-to-line popover, and paste-from-clipboard that unwraps an `_embody_tdn` envelope; the read-only viewer's jump menu now lists every top-level operator and annotation with type labels. Edit also accepts a replacement cover image (client-resized to 640x360).
- **Privacy: Inter is now self-hosted.** The four woff2 faces ship from `/fonts` and the Google Fonts CDN `<link>` is gone, so no visitor IP leaks to a third party. New **cookie notice** and **copyright/DMCA** pages round out the footer's policy set, alongside refreshed privacy and terms.

### Tests

- Test suite unchanged at **74 suites / 1,727 tests** -- the visualization split is runtime UI/camera behavior with no new Python unit coverage. The web added 2 Playwright e2e cases (multi-category submit + the 3-category cap).

## v6.0.55

A clipboard UX fix. Copying a COMP's network with `Ctrl+Shift+C` no longer immediately prompts to paste it back into TouchDesigner -- an *outbound* copy (you are exporting it to share or paste elsewhere) is now distinguished from an *inbound* TDN (the web "embody it" button, a shared envelope).

### Embody core

- **`Ctrl+Shift+C` (copy TDN) no longer turns around and offers to paste it back.** The clipboard auto-paste watcher polls `ui.clipboard` and offers to "embody" any new TDN it sees as a new COMP -- but it could not tell your own *outbound* copy from an *inbound* one, so copying a COMP to share it fired an immediate "Embody it into ... as a new COMP?" prompt. `CopyNetworkToClipboard` now seeds the watcher's last-seen signature with exactly what it wrote (re-read from `ui.clipboard` so it matches what the poll computes), so an outbound copy is recognized and skipped. An inbound TDN has different content -> a different signature -> still prompts, so paste-from-web is unaffected. Smoke-tested in the shipped `.tox` (a copy seeds the signature in the released build; clean boot, 0 errors). New `test_outbound_copy_does_not_prompt` + `test_inbound_after_outbound_still_prompts`.

### Tests

- Test suite **74 suites / 1,727 tests**, all green (+2 in `test_clipboard_watch` for the outbound/inbound distinction).

## v6.0.54

A crash-resilience build. **Embody now writes a cheap `.tdn` checkpoint of whatever changed after the agent (or you) goes idle** -- so a TouchDesigner crash loses little unsaved work, with no full project save and no freeze. Plus an opt-in live build visualization (watch Claude build, with a little builder-bot), threading guidance that stops agents over-engineering data fetches, and a web contribute-form fix.

### Embody core

- **Auto-save crash checkpoints.** A new always-on engine writes changed TDN COMPs to disk as a frame-cheap `.tdn` checkpoint a beat after the agent or user goes idle -- **no full project save, no TDN strip/restore, no frame freeze** -- so an accidental crash (often agent-induced during a heavy build) loses little, and the checkpointed COMPs rebuild on next open. The key was measuring where a normal export spends its time: the dominant cost of `ExportNetwork` is the `rglob` stale-file scan + cleanup (hundreds of ms), **not** the write (`_safe_write_tdn` is ~1.6 ms, serialization ~2 ms). A new `skip_cleanup=True` path on `ExportNetwork` skips the rglob, the stale-file cleanup, and the modal size/lock warnings, so a single-COMP checkpoint lands at **~3-6 ms** synchronous -- cheap enough to run inline on the main thread with no worker, no async, and no git churn from async-vs-sync output drift.
- **How it triggers.** Mutating MCP ops record the touched TDN COMP (walking up to the nearest tracked boundary) and arm a ~1 s idle-settle timer; on settle, the touched COMPs are checkpointed one-per-frame. A destructive `delete_op` of a child inside a tracked COMP also fires a synchronous pre-checkpoint *before* the delete, so a crash mid-delete still loses nothing since the last settle. `execute_python` / `exec_op_method` are deliberately **not** checkpoint triggers (their effects are unbounded and opaque -- skip-and-document), and `import_network` is excluded from the pre-risky path because its `.tdn` is the user's source-of-truth being reloaded, not state to overwrite.
- **Recovery on open.** In Export-on-Save mode (the default), reconstruction normally no-ops because the `.toe` is the source of truth -- but a crash means the `.toe` was never saved, so any TDN COMP that is present on disk (`.tdn` + a row in `externalizations.tsv`) yet **missing from the recovered `.toe`** is rebuilt from its `.tdn`. This works because `externalizations.tsv` is a `syncfile` DAT, so checkpoint rows reach disk within a frame *without* a project save. Recovery rebuilds nested TDN children with their own content (no empty shells), and a deleted COMP's tracking row is purged so recovery can't resurrect it.
- **Controls + self-heal.** A new **Auto-Save Checkpoints** toggle (default ON) and a read-only **Auto-Save Status** readout (Idle / Saved / Bypassed / Disabled) appear on the Embody COMP's TDN page; both self-heal onto a fresh install of the shipped `.tox` or an older `.toe` that predates them. The engine is **bypassed in Perform Mode and during saves**, and perf-gated so a checkpoint never piles onto a hot frame (it reschedules if FPS is under budget).
- **Verified by a 20-agent adversarial review** (10 codex exec + 10 claude sub-agents) that caught and fixed real defects pre-merge: a pre-risky checkpoint over `import_network(clear_first=True)` that would have **overwritten the user's just-edited `.tdn`** (data loss), nested-child recovery leaving an empty shell, a tracking-table mutation during the save window (crash), and an O(rows)-per-op lookup regression (now an O(1) keyed lookup). New `test_autosave` suite (**18 tests**).

### Envoy

- **Live build visualization (opt-in).** A new **Envoy Follow** toggle (default OFF, Envoy page) makes the network editor follow Envoy's work as Claude builds: within the viewed network it **glides** (ease-out) to center on each operator just touched; when the work moves to a COMP no pane is showing, it **navigates** a network-editor pane into that COMP and snaps to frame the op (you cannot glide across coordinate spaces). It **yields the instant you pan, zoom, or navigate** the view yourself and resumes once you stop. Main-thread only (driven from `_onRefresh`) and side-effect-free with respect to saved files -- it writes only pane/view state, which is never externalized.
- **The builder-bot ("embot").** While following, a small figure built from minimal networkbox annotations (head, eyes, body, arms, legs) **hops node-to-node** along a parabolic arc, hovers when idle, and does occasional gestures (a wave, an arms-forward reach, an arms-up pump, and now and then a full robot dance). Its color reflects "thinking time" -- cool cyan-green right after Envoy acts, warming toward red the longer the gap between ops -- and the touched node pulses the Envoy accent. The bot and pulse retire after ~30 s of quiet and are destroyed before each save, so they never externalize.

### Guidance

- **Stop agents over-engineering threading for TD data fetches.** `rules/td-python.md` and the `td-api-reference` skill (plus their shipped templates and a `skill-prerequisites` cross-link) gained a "Background and Long-Running Work" decision ladder: reach for the **Web Client DAT** (async, main-thread `onResponse` callback) or native JSON DAT -> DAT-to-CHOP chain for HTTP, the **Palette Thread Manager** only for genuinely blocking pure-Python work, and never a worker thread that touches a TD object or a `sleep`/`run()` poller. The CLAUDE.md template gained the matching pointer, and the published `td-development` threading docs were updated to match.

### embody.tools

- **Contribute form gates submit** until the required fields are filled.

### Tests

- Test suite **74 suites / 1,725 tests**, all green (`test_autosave`, 18, added for the checkpoint engine).

## v6.0.49

A generated-file-safety + web-polish build. Re-running Envoy's config generation (InitEnvoy, or flipping the AI Project Root) now PRESERVES your edits to generated rules/skills instead of clobbering them, via a content-hash drift manifest. The v6.0.47 annotation dedup reached Embody's own self-externalized `.tdn` files, and embody.tools got a deep TDN-viewer / Collection / YAML-viewer polish pass.

### Embody core

- **Generated files survive your edits (hash-detect).** `_writeTemplate` records a SHA-256 of each file it generates in `.embody/generated-hashes.json`. On regeneration (InitEnvoy, or flipping the AI Project Root) it now skips any generated rule/skill whose on-disk content no longer matches the recorded hash -- your edits win; delete the file to opt back into regeneration. Generated files stay byte-identical to their templates (sidecar manifest, no embedded hash); a legacy file with a marker but no tracked hash regenerates once, then becomes tracked and edit-protected. New tests B08-B12 in `test_claude_config`.

### TDN format

- **The annotation dedup reached Embody's own externalizations.** v6.0.47 made the exporter capture annotateCOMPs only in the compact `annotations:` array; saving the dev project re-exported `dev/embody.tdn` and `dev/embody/Embody.tdn` through that path, dropping **9 redundant `annotateCOMP` operator copies** plus their now-dead `type_defaults` / `par_templates` (840 lines removed) while keeping every annotation **byte-identical** in the `annotations:` section. Confirmed a safe dedup, not data loss, by a 20-agent adversarial review plus a field-by-field check (the `annotations:` block is identical before and after; 9/9 removed ops map 1:1 to a surviving native annotation).

### embody.tools

- **TDN network viewer.** Correct TouchDesigner family colors (TOP purple, CHOP green, SOP blue, POP blue-violet, MAT olive-gold, DAT pinky-purple, COMP grey -- read from `ui.colors`); op-reference parameters (a Feedback TOP's Target, etc.) now draw as **dotted edges arcing over the tiles**; node overlap is **always prevented** via a minimal nudge; a data wire that crosses an intervening same-row node **arcs above it**; the fullscreen control moved top-right and reveals on hover for card covers.
- **The Collection.** A **"by user" author filter** (mirrors the category facet, SSR-applied, shown only when there is more than one author); the toolbar + grid are encased in one panel; the Collection nav stays highlighted on a specimen page; the breadcrumb category links to a filtered collection.
- **Raw-TDN YAML viewer.** Block-sequence keys (`operators:`, `annotations:`) are now **foldable** -- their `- ` items sit at the same indent as the key, so they were wrongly read as child-less; line-number gutter alignment fixed; `+` / `-` icons on expand/collapse-all; a show/hide toggle on the disclosure.
- **Specimen page + chrome.** Badges + the "embody it" CTA moved to a sidebar to lift the network preview above the fold; equal-height columns and even section rhythm; the result thumbnail navigates to the specimen; a **3-10s page-load freeze fixed** (the nav-glass html2canvas snapshot was rasterizing the whole YAML viewer); sitewide OG metadata + contribute-form polish.

### Tests

- Test suite **73 suites / 1,707 tests**, all green (B08-B12 added to `test_claude_config` for hash-detect).

## v6.0.47

A TDN-format cleanup and save-UX build. Annotation COMPs are no longer double-captured in `.tdn` exports -- they lived both as a heavy `operators:` entry and in the compact `annotations:` array, dumping 100-205 lines of palette-clone boilerplate per annotation. The exporter now omits a `null` build number, the at-risk save check no longer mislabels a normal save as a test context, and all 12 affected gallery specimens were cleaned (-2,887 lines). Verified end-to-end: the shipped release `.tox` boots clean in a fresh-install smoke test with every fix live.

### TDN format

- **Annotation COMPs are captured ONLY in the `annotations:` array, never as `operators:` entries.** A stock TD annotate is a *palette clone* with an extension and ~40 custom parameters; serializing it as a regular operator dumped well over 100 lines of `custom_pars` (every `Opviewer*`/`Body*`) that exactly duplicated -- in a far heavier form -- what the compact `annotations:` entry already records: a single 205-line block for a single-annotate network, or a shared 183-line `par_templates` for a multi-annotate one. `_exportChildren` now skips any `annotateCOMP` child (even a non-utility palette clone, which is how they leaked in); the importer already rebuilds annotations from the `annotations:` array (Phase 7a), so the op-list entry was pure dead weight. The TDN spec and JSON schema gained an "Export Behavior" note documenting the guarantee.
- **`build: null` is omitted entirely.** Untracked / portable networks (no externalizations-table row, no `Build` parameter) previously emitted `build: null` in the header -- inconsistent with the format's omit-when-absent philosophy (`position`, `size`, etc.). Both export paths now drop the key when there's no build; older files carrying an explicit `null` still read fine.
- **12 gallery specimens cleaned.** Every affected `specimens/**` and `dev/specimen_lab/**` `.tdn` was stripped of its redundant annotate operators and the now-orphaned annotate-only `par_templates` / `annotateCOMP` `type_defaults` -- **2,887 lines of pure deletion**, validated by live re-import (every annotation rebuilds from the `annotations:` array with its title intact) and by clean reconstruction on project open.

### Save UX

- **The save-time "TDN Content at Risk" check no longer logs a misleading `[test]` warning.** During a project save, `_messageBox` saw the `_suppress_dialogs` save-window flag and mislabeled it as a test context, logging `[test] No response seeded for "TDN Content at Risk" ...` on every Ctrl+S whenever a TDN COMP held unprotected DAT content. The test gate (`_smoke_test_responses` seeded OR a runner active) and the save gate (`_suppress_dialogs`) are now separated: a real test still warns loudly so test authors notice an unseeded dialog, while a save returns the safe default quietly (DEBUG) -- no more textport spam. Return values are unchanged.

### Tests

- New **`test_tdn_annotation_export`** suite (7 tests): annotate excluded from `operators:`, present in `annotations:`, no heavy `custom_pars` dump, round-trips via the `annotations:` array, and `build` omitted-not-null. Two new **`test_dialog_suppression`** tests guard the save-vs-test split (a save stays quiet; a real run still warns). Test suite **73 suites / 1,702 tests**, all green; the shipped `Embody-v6.0.47.tox` passed a fresh-install smoke test (Embody/Envoy/TDN loaded, no script errors, both TDN fixes confirmed running live).

## v6.0.46

A docs-accuracy and web-polish build. A multi-agent audit swept the entire docs site, the AI machine-files (`llms.txt` / `for-ai`), and the README against the live source and fixed every stale claim; the embody.tools web app gained an app-native report dialog, a simplified specimen preview header, a centred contribute form (renamed `/submit` -> `/contribute`), and a themed 404; plus minor custom-parameter organization on the Embody COMP.

### Docs audit + reconciliation

- **Community-paste model corrected.** The platform docs (Collection / index / contribute) still described community specimens as pasting "inert by default" unconditionally -- stale since v6.0.44. They now describe the real verdict model: a **clean** specimen pastes live and fully working, a **flagged** one imports disarmed (provably-pure value expressions preserved), and a **blocked** one is rejected.
- **Counts reconciled to ground truth.** 72 test suites / 1,693 tests everywhere (testing.md's per-suite breakdown regenerated from real per-file counts); **49 MCP tools** across README, machine-files, and landing pages (was 48 -- `diff_tdn` was added after the count was last reconciled); `for-ai.json` version bumped to current.
- **API + shortcut fixes.** Wrong Manager shortcut (`Ctrl+Shift+O` opens it, not `Ctrl+Shift+E`, which exports); non-existent `tagOp()` -> `applyTagToOperator()`; `getExternalizedOps()` -> `getExternalizedOps(COMP)` (the method requires an op-family) -- corrected in the docs AND the shipped AGENTS template; the Claude Code rules/skills tables corrected to what Envoy actually generates (the 6 shipped rules, `+/visual-aesthetics`, no `.claude/commands/`); a broken `#parameters` anchor and a mistargeted Clipboard Auto-Paste link.
- **`llms-full.txt` TDN spec regenerated** from the current v2.0 specification (was a v1.3 snapshot -- missing the Back-compatibility section and the v1.4/1.5/2.0 changelog), ASCII-folded for the machine-file contract; the embedded MIME type corrected to `application/yaml`.

### embody.tools web

- **App-native report dialog** replaces the browser `prompt()` -- a themed `<dialog>` reason picker.
- **Specimen network-preview header** simplified: dropped the "{name} graph" title and the "inert preview" badge, styled to match the rendered-result panel.
- **Contribute page** (renamed `/submit` -> `/contribute`): app-styled `<select>` dropdowns with proper arrow spacing, centred form column, consistent with the manifesto.
- **App-native 404 page** replaces the default Astro 404.

### Build

- Minor custom-parameter organization on the Embody COMP.

## v6.0.44

Specimens from embody.tools now paste in LIVE and working, plus paste-placement and active-window fixes. The community safe-import was zeroing EVERY parameter expression -- a published specimen's GLSL uniform bindings, resolution, and animation drivers all collapsed to 0, so every pasted specimen rendered a dead frame. It now preserves provably-pure value expressions and disarms only genuinely side-effecting surfaces.

### Community paste: specimens paste in working

- **safe_import preserves pure value expressions.** `make_inert` no longer collapses every `=expr` / `~bind` to a constant. A new AST pure-value-expression allowlist (`scanner.is_pure_value_expression`) classifies an expression as safe iff it is provably side-effect-free -- par reads, `absTime`, `math.*`, `Par.eval()`, arithmetic, ternaries, `hasattr` -- and `make_inert` neutralizes ONLY expressions that are not (any side-effecting call, dynamic-attribute / dunder / lambda / comprehension / f-string escape, import, mutator method). Verified against a 70-case corpus: 29 benign idioms preserved, 41 attack patterns neutralized, including `op('x').destroy()`, `__import__`, walrus/lambda aliasing, and `__globals__`/mro escapes.
- **Scanner false positives fixed.** The denylist scanner flagged the standard TD idioms `parent().par.X.eval()`, `.store()`, and `tdu.*` as the Python builtins of the same name, and mis-scanned GLSL shader DATs as Python (a parse error counted as an execute surface). Parameter-expression danger now gates on the pure-value allowlist, and shader / data DATs (detected by `language` or file `extension`) are no longer AST-scanned as Python. The DoS bounds (AST depth / node-count / source-length) still block.
- **Live-if-scanned-clean routing.** `CollectionExt.PlanCommunityPaste` imports a `clean` specimen LIVE (no neutralization), and a `flagged` one inert-but-preserve-pure -- so a clean specimen pastes fully working with no warning.
- **TD palette extensions trusted, with hijack defense.** An extension resolving through a TD built-in palette shortcut (`op.TD<Name>` -- e.g. the standard Annotate COMP) is trusted (not disabled). Community `opshortcut` (global op-shortcut) registration is stripped on import so a malicious TDN cannot hijack a palette shortcut (e.g. register its own `op.TDAnnotate`) to repoint a trusted reference at attacker code; scoped `parentshortcut` is kept.
- **Adjacent surfaces closed.** Script DAT/CHOP/TOP/SOPs are bypassed (they run Python on cook), `tox_ref` / `tdn_ref` shells are stripped, the untrusted import runs with the target COMP's cooking suspended (closing the param-set-before-bypass-flag window), and `is_inert` is purity-aware.

### Paste UX

- **Pasted COMP auto-selects and the view pans to centre it.** It was landing off-screen / far-right. TD's network-view rectangle (`pane.bottomLeft`/`topRight`) reports stale coordinates from a script and `pane.home()`/`homeSelected()` are no-ops unless the pane is focused, but `pane.x`/`pane.y` (the network coordinate at the pane centre) IS writable -- so the new COMP is placed beside the network, selected on its own + made current, and the view is panned onto it.
- **The auto-paste prompt fires only while the TD window is active.** It was popping up while you were in the browser, and switching back left it stuck (the cursor-rollover signal only updates on a mouse-move). It now compares the OS frontmost-application PID to TD's own PID -- cross-platform (NSWorkspace on macOS, GetForegroundWindow on Windows, fail-open) -- and the latest clipboard wins when you return to TD.

### Tests

New `test_collection_pure` (14, in-TD: the validator, preserve-pure neutralization, scanner verdict, GLSL/script/tox_ref/opshortcut handling, and live-if-clean routing) and standalone `Collection/tests/test_safe_import_pure` (25); `test_clipboard_watch` gains an active-window-gate test. Affected suites verified green: collection safe-import (18), scanner (22), collection-pure (14), clipboard paste (42) + watch (6), plus the standalone 70-case validator corpus. Test suite **72 suites / 1,693 tests**.

## v6.0.42

Clipboard auto-paste: bring a TDN into your network with no keyboard shortcut. Embody now watches the OS clipboard and, when a TDN network appears (copied from the web "embody it" button, or Cmd-Shift-C on a COMP in TD), prompts to "Embody it" into the current network as a new COMP.

- **No-shortcut paste via a clipboard watcher.** The old Cmd-Shift-V paste binding is gone -- TD's native operator-clipboard paste fires on the same keystroke and cannot be intercepted or suppressed, so it pasted leftover TD nodes alongside the TDN. In its place, a generation-guarded `run()`-loop polls `ui.clipboard` (~1.5s) and, when a NEW `_embody_tdn` envelope appears, offers (via the Embody message box) to **Embody it** into the current network. It is debounced (one prompt per copy; a dismissed envelope never re-nags), gated on a new **Clipboard Auto-Paste** toggle (default on), skipped in Perform Mode, and the prompt self-suppresses during saves and tests. Copy (Cmd-Shift-C) is unchanged. New `test_clipboard_watch` (5 tests); `test_clipboard_paste` (42) green, no regression. Test suite **71 suites / 1,678 tests**.

## v6.0.41

The git-uncommitted status axis: the manager gains a second status axis, completing the v5.0.437 feature set on the v6 line (after diff_tdn in 6.40). Externalized files saved to disk but not yet committed to git now show a distinct orange Strategy badge, kept separate from the red "unsaved" axis.

- **Second status axis -- git-uncommitted.** Externalized DAT scripts use TD's bidirectional syncfile, so they are always in sync with disk -- their only meaningful "changed" state is git-relative (on disk but not committed). A `git status --porcelain` scan runs ASYNC on a worker thread (no refresh-frame drop; `--no-optional-locks` so it never contends with a concurrent commit), maps the changed files to operator paths via pure string math, and stores the result at runtime (never written to `externalizations.tsv`, which would churn). The manager renders a distinct orange `Uncommittedcolor` badge for TOX/TDN/DAT alike, overriding only the SAVED states (red unsaved + amber par-change keep precedence). Self-disables outside a git repo. The engine (`_findGitRootSync`, `_parseGitPorcelain`, `_mapChangedToOps`, `_rowHasChanges`, `_updateGitStatus`) is generation-guarded so a stale worker cannot clobber a newer scan.
- **A `changed` filter keyword + a refresh-after-commit rule.** Typing "changed" in the manager filter shows only rows with pending changes on EITHER axis -- unsaved (dirty/Par) OR git-uncommitted -- via the single-source-of-truth `_rowHasChanges`. A shipped `refresh-after-commit.md` rule reminds agents to refresh the manager after a git commit so the orange badges clear.
- **Adapted to v6 + verified.** The async scan uses `op.TDResources.ThreadManager`; the `Uncommittedcolor` param (already present in v6 from a partial attempt) is now fully wired. Backend logic is covered by `test_git_status` (20 tests), and the full data path was verified live (scan -> git_status storage -> lister git_state column -> orange badge). Test suite **70 suites / 1,673 tests**, all green.

## v6.0.40

The diff_tdn release: re-integrates the `diff_tdn` MCP tool and its companion `.tdn` git diff driver -- shipped on v5.0.437 but never present on the v6 line -- into v6's YAML v2.0 world, with a PyYAML-in-venv fix the YAML textconv needed and a 4-lens adversarial review that caught two real regressions before merge.

### diff_tdn -- see what's unsaved, in one COMP or the whole project

- **`diff_tdn` MCP tool -- the UNSAVED view git can't give.** It compares the live in-memory network against its on-disk `.tdn`, answering "what have I changed but not saved?" -- something git fundamentally cannot see (git only reads files on disk, never TD's live state). `target` accepts a COMP path **or** a `.tdn` file path/bare filename (e.g. `"tooltip.tdn"`, resolved to its COMP) for one COMP in full per-field detail; **omit `target` for a whole-project summary** across every live TDN COMP (which changed + counts). The comparison is **semantic, not byte-level**: both sides normalize through the same `type_defaults`/`par_templates` expansion the format uses, and the volatile export header (`build`, `generator`, `td_build`, `exported_at`, `source_file`) is ignored. Each change is `{old, new}` with **`old`=disk, `new`=live**, tagged `kind: root | op | annotation`. The engine lives in `TDNExt` (`DiffLiveVsDisk`/`DiffAllLiveVsDisk` + the pure `_diff_normalized`); `EnvoyExt._diff_tdn` is a thin main-thread delegate.
- **Companion `.tdn` git textconv driver for committed / history diffs.** A raw `git diff` of a `.tdn` is buried in export-header churn (a re-export bumps the timestamp/build even when nothing changed). Embody now installs a git **textconv** driver (`.gitattributes` `*.tdn diff=tdn`, `.embody/tdn_textconv.py`, and `git config diff.tdn.textconv`, auto-configured on Envoy startup) that strips the volatile header before diffing -- so `git diff` / `git log -p` / `git show` on a `.tdn` show only real network changes, and a no-op re-export shows nothing. `diff_tdn` covers the unsaved window git can't see; the driver covers the committed view git owns.
- **Adapted to v6's YAML v2.0 `.tdn`, with a real PyYAML venv fix.** Unlike main's JSON `.tdn` and pure-stdlib textconv, v6's `.tdn` are YAML, so the textconv is YAML-aware -- and git invokes it via Embody's venv python, which lacked PyYAML and silently fell back to a raw (noisy) diff. `pyyaml` is now a venv dependency and `_environmentNeedsInstall` detects its absence to upgrade existing venvs. The diff engine also reconciles a legacy v1.5 array-of-lines `dat_content` with the v2.0 joined-string form so an unchanged DAT does not false-diff across the format bump.
- **Discoverability: `get_externalizations` / `get_externalization_status` recommend `diff_tdn`.** Each externalization row now reports its `strategy`, `absolute_path`, and a `recommended_tool: diff_tdn` hint for TDN COMPs; the MCP tool reference documents when to reach for `diff_tdn` (unsaved) versus `git diff` (committed, kept clean by the driver).
- **Reviewed by a 4-lens adversarial panel that caught two real regressions pre-merge.** The panel (spec-fidelity, correctness, TD-safety, integration) flagged a dropped `_get_externalizations` enrichment and a `_environmentNeedsInstall` change that broke four existing setup-environment tests -- both fixed and verified. New suites `test_tdn_diff` (11) and `test_tdn_diff_engine` (25, including the dat_content reconciliation) cover the full handler chain and the pure engine. Test suite **69 suites / 1,653 tests**, all green.

## v6.0.39

The save-resilience release: a `project.save()` no longer freezes TouchDesigner with onboarding modals, and a long-standing watchdog bug that let the Envoy MCP server stay wedged after a save is fixed at the root -- the server now self-heals in about a second. Plus comprehensive v6 test coverage (169 new tests across 9 suites).

### Envoy: the liveness watchdog now actually self-heals a save-time wedge

- **Root cause: the watchdog's revive cooldown compared a per-launch frame counter against a value saved across launches.** `_reviveDeadServer` measured its ~2s anti-spam cooldown in `absTime.frame` (frames since the app launched -- resets to 0 every launch) but stored that value in COMP storage, which persists into the `.toe`/`.tdn`. A high frame value baked from a prior session made every revive compute a negative `now - stored` delta -- always "less than 2s ago" -- so the guard returned **before scheduling the restart, every single time**, permanently. The watchdog detected the wedge forever but was structurally forbidden from fixing it. The cooldown now uses `time.monotonic()` on an instance attribute (never `absTime.frame`, never storage); `__init__` scrubs the obsolete `_last_revive_frame` key. A fresh launch always starts un-wedged. A regression test stores a high frame and asserts the revive still fires.
- **The watchdog now trusts the socket, not internal flags.** It keyed off `_init_complete` and `_starting`, both of which a `project.save()` resets -- so the tick went idle and never revived a genuinely dead server. It now keys off the visible `Envoystatus` plus a real socket probe: a dead socket while enabled revives regardless of those flags. `Installing deps...` is the one grace state it will not interrupt.
- **`Start()` no longer trusts a stale "Running" status.** It bailed if the status merely *said* "Running"; a worker that died without updating the status short-circuited the restart. It now probes the socket first and restarts on a dead one.

### Envoy: the onboarding dialog never fires during a save or a test

- **`project.save()` used to surface the "Enable Envoy?" modal (sometimes many times), freezing TD.** A single predicate `EmbodyExt._suppressDialogs()` -- true while a test run is active OR a save is in progress -- now gates the queue site in `Verify()`, the deferred `_promptEnvoy`, and `_messageBox` itself, so the prompt can neither show nor queue mid-save. `onProjectPreSave` sets a `_suppress_dialogs` flag for the save window, scrubbed on next open so it never bakes a permanent suppression into the `.toe`. The file-cleanup and deprecated-externaltox prompts are gated the same way; `_promptEnvoy` treats a suppressed (`-1`) return as a no-op so a seeded test answer is still honored.

### Tests: comprehensive v6 coverage

- **169 new tests across 9 suites** (67 suites / 1,616 tests total): clipboard copy/paste (42), collection scanner (22) + safe-import (18), v6 hardening (20), specimen publish (19), the Envoy liveness watchdog (21), GLSL externalize (11), layout lint (10), and dialog suppression (6), plus `test_smoke_release` additions.
- **Layout lint `maxDepth` fix.** The v6.0.34 `execute_python` layout lint called `findChildren(depth=12)` (exactly depth 12 -- matched nothing); it now uses `maxDepth=12`, so the lint actually fires.

## v6.0.34

Everything since v6.0.26 in one release: a GLSL-shader externalization fix so shaders write as `.glsl` instead of `.py`, the recurring `execute_python` "(0,0) pileup" now caught by a layout lint at the Envoy tool layer, a self-contained Specimen publish hook for the embody.tools "embody it" copy-paste, and a waveform-stack feedback cook-loop fix — plus six landscape transmission specimens.

### Externalization

- **GLSL shader DATs now externalize as `.glsl`, not `.py`.** `EmbodyExt._externalizeDATs` inferred each DAT's externalization tag from a bare `dat_type_to_tag` map where `['text'] = 'Pytag'` — so every text DAT, GLSL shaders included (type `text`, language `glsl`), was written out as `.py`. It now resolves the tag from the DAT's *content* via `_inferDATTagValue` (which reads the text DAT's language/extension), so a shader externalizes with the correct `.glsl` extension. This was the bug behind the content-safety "Externalize DATs" path mis-tagging shaders as Python. The 8 newer Specimens' 42 shaders were re-externalized to `.glsl` to match the 4 older ones.

### Envoy: layout lint at the tool layer

- **`execute_python` now warns when it leaves operators at (0,0), overlapping, or with docked DATs scattered.** `create_op` auto-positions; `execute_python` (raw `comp.create()` / `.copy()`) did not — the recurring source of new operators piled at the origin. Envoy now snapshots the op tree before running your code and lints only the operators the call creates: a new `_lintLayout` flags ops stacked at (0,0), overlapping op pairs, and docked DATs more than 500 units from their host, and `_lintNewOps` emits a `LAYOUT WARNING` on the response (via the notable-logs piggyback). `network-layout.md` and its shipped template were DRY'd to state the trap once around the new enforcement and collapse the duplicate anti-pattern bullets.

### embody.tools: Specimen publish

- **`specimen_publish.py` — a project `onProjectPostSave` hook** that exports each manifest Specimen self-contained (DAT scripts embedded) to `specimens/<tdn_path>`, the form the embody.tools "embody it" copy-paste consumes. Unchanged files are skipped, so a save only rewrites the specimens that actually changed.

### Specimens

- **Waveform-stack feedback fix.** `specimen_lab/waveform_stack` had a cook-dependency loop — the Feedback TOP's output wired back into its own input. Broken by seeding the Feedback TOP from outside the loop (`res_fb`) and grabbing the frame-delayed state from its Target TOP, the correct bounded-feedback pattern.
- **Six landscape transmission specimens** added to `dev/specimen_lab` (4K `Resw`/`Resh` control, shaders embedded): essence-streams, vertical-fibers, crosspoint (VHS glitch), waveform-stack (bounded feedback, up to 512 lanes), packet-fabric (GPU POP sim), and hyper_ntsc (NTSC chroma-bleed / dot-crawl); reaction-diffusion was landscaped with a bounded sim.

Test suite **58 suites / 1,439 tests**, no regressions.

## v6.0.26

A correctness + efficiency release that also finishes the TDN clipboard Copy/Paste loop: a critical TDN round-trip fix, the pre-save "TDN Content at Risk" dialog no longer firing on annotated specimens, the Envoy save-time watchdog log storm fixed for real, a four-part MCP token-efficiency pass, a fourth Specimen (a GPU flocking "Murmuration"), the Copy half of the clipboard wired to Cmd-Shift-C, raw-`.tdn` paste, and a POP point-sequence import fix.

### TDN clipboard, paste & naming

- **`Cmd-Shift-C` copies the selected COMP to the clipboard.** v6.0.11 shipped `Cmd-Shift-V` paste and claimed a "Copy tdn button in the tagger" -- but that button never existed and `CopyNetworkToClipboard` had **zero callers**, so a user had no way to *copy*. The copy half is now wired: `CopySelectedToClipboard` (Ctrl/Cmd-Shift-C) exports the COMP selected in the current network to an `_embody_tdn` envelope on the OS clipboard. The loop is finally symmetric.
- **`Cmd-Shift-V` now accepts a bare `.tdn` document**, not just an `_embody_tdn` envelope -- so a `.tdn` file's text copied from an editor pastes in. A bare `.tdn` carries no provenance, so it is **sandboxed** (scanned + default-inert) exactly like community content: a pasted stranger's `.tdn` cannot run code. For a trusted local file, `ImportNetworkFromFile` imports it live. Parses YAML v2.0 and legacy JSON.
- **The clipboard envelope is pretty-printed.** `to_clipboard_str` switched to `indent=2`, so a pasted envelope is human-readable. The `sha256` is computed over the canonical inner `tdn` (sorted keys, no spaces), never the clipboard string, so indentation changes nothing about integrity or web byte-parity.
- **A pasted COMP is named from the TDN's `network_path`** basename (e.g., `/specimen_lab/noise_terrain` -> `noise_terrain`), sanitized via `tdu.validName` with collisions uniquified -- no more `pasted_tdn`. No spec change: `network_path` is required, so it already carries the name.

### Fixes

- **POP point-sequence `numBlocks` import fix.** Pasting/importing a TDN with POP point sequences (e.g. a `primitivePOP`/`linePOP` `pt` sequence) logged `Failed to set numBlocks=N ... 'NoneType' object has no attribute 'numBlocks'` and dropped the points. Cause: `op.seq['name']` (subscript) silently returns `None` for POP sequences while iteration finds them (and attribute access raises). Export read them fine via `par.sequence`, but import used the broken subscript. A new `_getSequenceByName` helper resolves sequences by iteration; all three import sites route through it. Regression test added; verified on `noise_terrain` (point counts 2/5/6/8 restored exactly).
- **`test_tdn_file_io` + `test_tdn_helpers` updated for TDN v2.0 YAML.** The v2.0 migration (v6.0.16) switched exports to YAML but left these 2 suites parsing with `json.load`, so 33 tests had been red ever since (30 `JSONDecodeError`s + 3 assertion fails). They now parse with `yaml.safe_load`, and `_read_existing_tdn` rejects non-dict results (YAML parses garbage like `not valid json {{{` into a scalar string where legacy JSON would have raised). Suites back to green (92/92 + 53/53).

- **TDN custom-parameter VALUES now round-trip.** Exporting a COMP with custom parameters and re-importing it silently reset every value to 0/min: the exporter omits a value that equals its default (intentional minimization), but the importer created the parameter with the right `.default` and never set `.val`. So a default-valued custom par imported inert -- which broke **every parametric specimen** on import (noise-terrain's 10 params, murmuration's 10). Fixed with a default->value fallback in `_setCustomParValues`: when no explicit value is stored, single-component non-pulse pars initialize from their default; expression/bind values (which always carry a value) and multi-component defs are untouched. 6 new regression tests cover root + child COMPs, default + non-default, Float/Int/Toggle, and an expression-mode guard.
- **The save-time watchdog log storm is gone.** A `project.save()` reinitializes EnvoyExt many times in a rapid same-frame burst; each reinit armed a liveness-watchdog tick, and ~4s later they all came due in one frame and each revived + logged -- the "MCP socket on port None unreachable -- reviving server" line repeated 18-21x per save, plus an equal pile of redundant Start() schedules. The per-instance identity guard couldn't dedupe them because the `run()` reschedule string re-resolves to the current instance. Fixed two ways: a monotonic generation token collapses the leftover tick *loops* on the next reinit (so they don't accumulate over a session), and -- the real fix for the same-frame burst, where the generation counter doesn't accumulate -- a short frame-cooldown in `_reviveDeadServer` collapses all same-frame revives to **one** log + one revive. The genuine self-heal (revive after a save/reinit that left Envoy down) is preserved; verified on a real save (21 warnings -> 1).
- **`test_claude_config` skills-count corrected** (7 -> 8) -- `visual-aesthetics` had been added to the shipped-skills map without updating the count assertions.
- **TDN content-safety scan ignores palette-clone internals.** The pre-save "TDN Content at Risk" check walked into palette-clone COMPs and flagged their internal DATs/storage -- e.g. an annotateCOMP's button `help` tables -- as user content at risk, so any TDN-tagged COMP containing annotations (every annotated Specimen) popped the dialog on *every* save. Both scans (`_findAtRiskDATs`, `_findAtRiskStorage`) now skip anything inside a palette clone: a clone's internals are regenerable palette boilerplate, never authored content. Verified live (11 at-risk -> 0).

### MCP token efficiency

The Envoy MCP server was a large share of per-session token usage because tool *results* stay in context. Four changes cut response size dramatically:

- **`_logs` piggyback is now WARNING/ERROR-only.** Every response previously glued up to 20 recent log entries on -- almost all routine INFO noise ("Processing:", the echoed code, "completed successfully") -- riding along on hundreds of calls per session. Now a `_logs` field appears only when a WARNING/ERROR was logged during the call (capped ~8); the served-cursor still advances so nothing is re-served. `get_logs` remains the full-history escape hatch.
- **`run_tests` returns counts + failures only.** The full suite's ~1,400 per-test PASS objects (~100k tokens in one response) are dropped; you get the totals and only the non-PASS results. Full per-test detail is in the test log file under `dev/logs/`.
- **`export_network` to a file returns a compact summary** (op/annotation counts + file path), not the whole `.tdn` echoed back -- Read the file for details (which CLAUDE.md already prefers).
- **`capture_top` no longer inlines the image by default.** Base64 previews are token-heavy; it returns the saved file path (Read it to view). Pass `inline=true` for an embedded preview.

### Specimen Collection

- **Murmuration** (4th specimen) -- a dense GPU particle swarm that flocks like a starling murmuration at dusk. True per-neighbor Reynolds flocking on the GPU: a Neighbor POP emits each point's neighbor *index list*, and a GLSL POP iterates those real neighbors for cohesion, alignment, and inverse-square separation (the key to even spacing -- a centroid-only force cancels inside a symmetric clump), plus a slow moving attractor, curl-noise wander, soft containment and drag. Rendered as additive point sprites with a speed-mapped dusk color ramp + bloom. Fully parametric (10 params), purely procedural, zero errors. The `specimen-authoring` skill gained a GPU-particle/flocking section (POP feedback loops, neighbor-list GLSL iteration, point-sprite rendering, the startpulse transport, and the TDN param-value gotcha).

## v6.0.16

The on-disk `.tdn` format graduates to **TDN v2.0: YAML**. Networks now serialize as a single self-contained YAML document instead of JSON, so a `.tdn` reads top-to-bottom like the network it describes — and your shaders and scripts read like code, not escaped strings.

### TDN v2.0: the file format is now YAML

- **YAML, a strict JSON superset.** A `.tdn` is now one YAML document. Multi-line `dat_content` (GLSL, Python, any `textDAT` script) is stored as a plain string rendered as a YAML literal block scalar (`|`), so the source reads top-to-bottom with no escaped newlines and git diffs it line-by-line. This reverts the v1.5 array-of-lines workaround — the block scalar does the same job natively, and more readably. Short numeric vectors (position, size, color) stay inline (`[200, -100]`); longer or non-numeric sequences use block style.
- **Lossless and deterministic.** Round-trips are byte-exact: trailing-newline count is preserved through automatic `|` / `|-` / `|+` chomping, and the output is stable across re-dumps (no key reordering, no anchors) so re-saving an unchanged network produces no diff. Verified byte-identical on the shipped specimens including real shader text, tabs, and trailing newlines.
- **Reads legacy JSON — no migration gate.** Existing `.tdn` (versions `1.x`/`1.5`, written as tab-indented JSON) still import unchanged. Importers parse **json-first**: a document starting with `{` or `[` is read by the JSON parser (after stripping any leading UTF-8 BOM and whitespace), and only otherwise by YAML — so back-compat does not depend on a YAML C library, and tab-indented legacy files (which YAML forbids as indentation) load losslessly. Migration is lazy: a JSON `.tdn` is rewritten as YAML the next time Embody saves it.
- **Smaller files via boilerplate omission.** Auto-created default docked compute DATs (the "Example Compute Shader" companion TD spawns alongside a `glslTOP`/`glslmultiTOP`) are no longer serialized when unchanged — TD recreates the exact default on import. Combined with the YAML representation, files are roughly **17% smaller**. The MIME type is now `application/yaml`.
- **One-time migration diff.** The first re-save of an existing JSON `.tdn` produces a one-time whole-file diff as it converts to YAML; this is expected and benign. A v2.0 YAML file cannot be read by a pre-2.0 Embody build (JSON-only) — new builds read old files, but not the reverse.

### Docs

- The [TDN Specification](tdn/specification.md), [format overview](tdn/index.md), [examples](tdn/examples.md), [import/export](tdn/import-export.md), [schema guide](tdn/schema.md), and [supported formats](embody/supported-formats.md) are rewritten for v2.0 YAML, with back-compatibility, literal-block `dat_content`, chomping, and boilerplate-omission documented. `tdn.schema.json` validates the parsed structure, identical for YAML and JSON sources.

## v6.0.11

Embody v6's Envoy + agent-guidance release: an MCP connection that self-heals across saves and reinits, the clipboard Copy/Paste loop with community-TDN safety, and new always-loaded guidance (crash avoidance + visual aesthetics) that deploys into user projects.

### Envoy: the connection self-heals (the end of "connected:false")

- **Liveness watchdog, tied to the EnvoyExt instance lifetime.** The long-standing "connection dropped while TouchDesigner keeps running" symptom is fixed by a pure `run()`-loop that probes the MCP socket every ~4s and revives Envoy whenever it is enabled-but-down — a dead socket, OR a `project.save()` / extension reinit that took the server down — force-freeing port 9870 if it is still held and rebinding in ~1s, with no restart and no manual toggle. It is armed from `__init__` (one loop per instance, dying only when a reinit replaces the instance, whose `__init__` arms a fresh one), so a save's mid-cycle reinit — which suppresses the old server thread's exit callback (no `_scheduleRestart`) and can skip or race the new instance's auto-start — can no longer orphan it. The earlier `Start()`-armed approach missed exactly this case. A stuck-`_starting` guard forces a revive if the startup poll loop dies; a tick error never kills the loop. Verified: killing the live listener self-heals in ~6s, and three consecutive `project.save()` cycles each had the watchdog fire (`running=False`), force-free the held port, and rebind in ~1s — the exact scenario that previously left the server permanently down.

### Clipboard: Copy/Paste TDN, with community-source safety

- **Copy/Paste networks through the TDN clipboard.** Copy a COMP's network to the clipboard as a portable `_embody_tdn` envelope (the **Copy tdn** button in the tagger); paste it back with **Ctrl+Shift+V** as a new COMP. The clipboard pure-logic now lives INSIDE the Embody COMP (portable in the `.tox`), not a loose folder.
- **Community TDN defaults to inert.** Your own TDN pastes apply directly (trusted); TDN whose source is `embody.tools` (the community gallery) is run through a capability scanner and defaults to inert — Execute DATs disarmed, expressions neutralized, IO operators bypassed, storage stripped — while the content is preserved for inspection. Implemented as a `CollectionExt` extension plus self-contained `scanner` / `safe_import` DATs, with envelope hashing byte-compatible with the web contract.

### New agent guidance (rules + skills), deployed to user projects

- **`performance.md` (new, always-loaded).** Crash/freeze avoidance: a metric-gating protocol around heavy builds (baseline `get_project_performance`, re-check after each step, localize with `get_op_performance`), stop conditions with thresholds, a wiki-cited crash-cause table (resolution explosions, unbounded feedback, always-cooking operators, GLSL crashes, GPU/CPU exhaustion), and safe-default caps. Driven by wiki-verified TD performance research.
- **`visual-aesthetics` (new skill).** Objective composition / value / color / contrast / motion / finishing guidance — each as principle, TD technique, and failure mode — plus a mandatory `capture_top` preview-and-judge loop: never declare a visual task done on a black frame.
- **Preview-and-judge** reinforced across `create-operator`, `debug-operator`, `mcp-tools-reference`, and `CLAUDE.md`. **`td-connectivity`** now documents the watchdog and "don't restart on a drop — let it self-heal." **`network-layout`** is hardened against the `execute_python` / `.create()` (0, 0) placement bypass.
- All of the above deploy into user projects via the template map (`performance`, `visual-aesthetics`, and `td-connectivity` newly registered).

This is the first changelog entry for the Embody/Envoy (TouchDesigner) side of v6; earlier v6.0.x builds were the embody.tools platform (web gallery, server-side scanner, backend) under `platform/`.

## v5.0.429

A friendlier "Duplicate Path Detected" dialog: a naming convention that auto-resolves the common template-plus-copies case, a strategy prompt for oversized groups, and self-labeling buttons — so you can finally tell which operator is which.

### Duplicate path resolution

- **Feature: `Template Master Name` convention auto-resolves duplicates.** New `Templatemaster` parameter on the Embody COMP (default `__template__`). When a group of operators sharing one external path has **exactly one** whose path contains that name as a whole segment (e.g. a `__template__` parent COMP), it is auto-selected as the master and the rest are tagged `clone` — no dialog. This targets the common app-generated pattern of one template plus many runtime copies (e.g. a `scene_<id>` chain where each copy carries the template's externalized DATs). Opt-in by convention: projects that don't use the name see no change and still get the manual prompt; set the parameter to your own convention (e.g. `_master`) or clear it to always choose by hand. Matches a whole path segment (not a substring), and only when exactly one operator matches — 0 or 2+ are ambiguous and fall through to the prompt. Persisted across upgrades via `config.json`. Implemented as `_resolveByTemplateMarker`, wired into `checkForDuplicates` after the clone/replicant resolvers.
- **The manual prompt no longer shows N identical buttons.** Operators in a duplicate group usually share a name, so every selection button used to read the same (e.g. eight `fbx_callbacks` buttons with no way to tell them apart). Buttons are now labeled by the path segment that **differs** across the group, numbered to match the dialog body — `1: __template__`, `2: scene_1exalohf`, … (`_duplicateButtonLabels`).
- **Large groups get a strategy prompt instead of an unreadable button row.** Above `_MAX_MANUAL_BUTTONS` (5) operators, a button per operator overflows the dialog, so the prompt switches to **Keep first as master** / **Dismiss** and points at the Template Master Name convention for hands-off resolution next time (`_promptForLargeDuplicateGroup`).

### Tests & docs

- **Test: 1,413 tests** (+12 in `test_duplicate_handling.py`): convention resolution (single / zero / multiple / empty / custom-marker / exact-segment), the large-group threshold (at-threshold enumerated vs. above-threshold strategy and dismiss), and button-label disambiguation.
- **Fix: `test_envoyenable_reflects_server_state` skip-list.** Its transitional-state skip set (`Waiting`/`Starting`/`Stopping`) predated the `"Restarting after reinit..."` status added by the v5.0.428 Envoy work, so it could fail during that settle window. Added `Restarting`/`reinit` so it skips — rather than fails — that transitional state (note `"Starting"` is not a substring of `"Restarting"`).
- **Docs**: new auto-resolution and button behavior documented in [Duplicate Path Handling](embody/externalization.md#duplicate-path-handling); `Template Master Name` parameter added to [Configuration](embody/configuration.md).

## v5.0.428

Everything since v5.0.414, bundled into one release. The headline is **`tdn_exclude`** — a tag that makes a COMP invisible to the TDN system — alongside a **rebuilt TDN dirty-detection pass** that finally notices parameter edits without churning on live expressions, **Envoy resilience hardening** (honest startup status, a `restart_td` zombie fix, status relocated into the window header), a silenceable save-time content-safety dialog, **three issue #21 crash fixes** hardened across the whole table-read surface, a calmer first launch, and a final regression-review pass that corrected several rough edges before release. **57 test suites / 1,401 tests, all passing**, plus a fresh-install smoke test of the release `.tox`.

### TDN exclude tag

- **Feature: `tdn_exclude` — opt a COMP out of the TDN system.** A new `Tdnexcludetag` parameter on the Embody COMP (default `tdn_exclude`) defines a tag that makes a COMP invisible to TDN: never exported (no `tdn_ref`/`tox_ref`, no structural reference), never stripped on save, never destroyed/recreated by `ReconstructTDNComps`'s `clear_first` import. **Primary use case: cascade-autotag bypass** — when `Tdncascade` is on, tagging a parent `tdn` propagates to every child; `tdn_exclude` is the durable opt-out for app-managed children (spawned via `op.copy()` at runtime, populated from user data — e.g. Moonshine's `proj_<id>` chains). Runtime `.copy()` clones inherit the tag and stay invisible. Annotation COMPs are ineligible. `getTags` filters the exclude tag out of its selectors *by parameter name*, so naming it identically to a real tag never drops the real tag. Implemented across `EmbodyExt` (strip, dirty-detection, at-risk walks, cascade, `_getTDNStrategyComps`) and `TDNExt` (`_hasExcludeTag`, export, `_collectAllPaths`, `clear_first` preservation). Docs: [Excluding a COMP from TDN](embody/externalization.md).
- **Exclusion is honored at a TDN boundary's direct children; nested excluded COMPs are preserved, never lost.** The strip/clear passes preserve an excluded COMP only when it's a direct child of the exported boundary. A COMP tagged for exclusion but nested under a non-excluded intermediate cannot be preserved by those passes — so rather than dropping it from the export while the strip destroys it (silent data loss), Embody now **serializes it as ordinary content** (it round-trips and survives) and warns that the tag had no effect at that depth, naming the COMP to tag instead. Export, fingerprint, strip, and `clear_first` import all apply this rule consistently.

### TDN dirty detection

- **The dirty indicator notices parameter edits — without churning on live expressions.** The per-COMP fingerprint now includes each operator's non-default parameters (its own custom pars and child operators'), recording the **authored** value — `expr` for expression mode, `bindExpr` for bind, `val` for constant — never `par.eval()`. This matches exactly what an externalized `.tox`/`.tdn` serializes, so an *authored* edit flags dirty while a dependency-driven change to a *live expression's evaluated value* (a parameter bound to `absTime.frame`, an audio level, a moving CHOP) does **not** — eliminating perpetual false-dirty re-export churn on animated COMPs. The same authored-capture rule governs `ParameterTracker.captureParameters` (the TOX path), so both dirty mechanisms agree and neither has cook side effects. About-page metadata (Build/Date/Touchbuild) is excluded so build bumps don't dirty the COMP. Baselines are primed at the deterministic clean moments — right after externalize and after reconstruction.
- **One fingerprint sweep per Refresh.** Dirty detection previously fingerprinted every TDN COMP *twice* per Refresh (an inline loop in `Update` plus `dirtyHandler`) and re-scanned the externalizations table once per COMP per call — a visible frame hitch on large networks. It's now a single sweep in `dirtyHandler`, with `tdn_paths`/exclude-tag computed once and reused, and the redundant per-COMP `compareParameters` pass for TDN COMPs removed (the fingerprint already covers parameters).
- **`DirtyCount` reads the fingerprint result for TDN COMPs.** It previously used live `oper.dirty`, which is always `True` for a TDN COMP (empty `externaltox`), so every clean TDN COMP showed as dirty in the UI badge. It now trusts the table's fingerprint-derived `dirty` value for TDN-strategy COMPs (and still uses `oper.dirty` for TOX).
- **A reverted edit clears the dirty flag.** The passive scan set `dirty` when a COMP changed but never cleared it when the COMP became clean again, so the indicator stuck on after a revert. It now clears the flag when the fingerprint matches the baseline.
- **Fix: `_openFileLocation` no longer logs a false warning on Windows** — `explorer /select` returns exit code 1 even on success; switched to `subprocess.Popen`.

### Envoy resilience

- **Envoy startup status tells the truth.** Status no longer reads "Running on port N" optimistically before the server has bound. Start waits for a real readiness handshake (a worker-thread monitor sets `startup_event` from uvicorn's `started` flag; a deadline-bounded main-thread poll flips status to "Running" *only after* a confirmed bind), and startup failures — including a uvicorn bind error raising `SystemExit`/`BaseException` — route to the error path. A per-generation `_starting` guard prevents duplicate concurrent starts.
- **Envoy status moved into the window header, prefixed "Envoy".** The standalone toolbar status widget is gone; live state now renders in the top header as "Envoy Running on port N" / "Envoy Disabled" / "Envoy Error: …", prefixed so it reads unambiguously. The stored status par value stays unprefixed, so EnvoyExt's status checks are unaffected.
- **Fix: `restart_td` no longer false-matches zombie or foreign processes.** Bridge process discovery now validates via `ps` (`_process_is_real_td`) that the process isn't a zombie and its executable basename is actually `TouchDesigner`, instead of a loose `pgrep -f TouchDesigner` match.

### Save-time content safety

- **The "TDN Content at Risk" dialog can be silenced for good.** When a save would drop DAT content or storage from a TDN COMP, the warning now offers a persistent "Always Skip" (sets `Tdndatsafety = 'ignore'`) alongside "Always Externalize" — both reversible via the `Tdndatsafety` parameter.

### Externalizations table

- **`externalizations.tsv` no longer churns phantom timestamp rows per save.** `checkOpsForContinuity` was bumping the `timestamp` column on every row each save (writing the externalized file's mtime, which the strip/restore cycle bumps for every `.tdn` regardless of content) — ~330 lines of diff noise per commit. The continuity scan no longer touches timestamps; the column now reflects only explicit Save/SaveTDN/rename events. Trade-off: an out-of-band edit (e.g. `git pull` brings a new `.tdn`) won't auto-update the TSV timestamp — pulse `Refresh` to sync.

### Crash safety (issue #21)

- **`captureParameters` no longer crashes on broken expressions.** Reading authored values (`expr`/`bindExpr`/`val`) instead of `par.eval()` means a broken expression (`ext.NotYetLoaded.X`, `op('./missing')`, palette-clone expressions) can't raise during the dirty scan.
- **`_cellVal` guards every externalizations-table read.** TD returns `None` for a missing column or a row-key miss, and `.val` on it raised `AttributeError` — the issue #21 crash. The `_cellVal(row, col, default='')` helper was applied across the entire `EmbodyExt` table-iteration surface (not just the 5 sites the tracebacks pointed at), so the migration/continuity/dirty/dedup paths run against a partial or legacy table without crashing. It also **logs a warning** on a genuine row-level inconsistency (a short/partial row whose column exists in the header) so silent table corruption surfaces, while staying quiet on the normal not-found and legacy-missing-column cases.
- **`onProjectPreSave` no longer truncates the `.toe` to 0 bytes on an unhandled exception.** The entire externalization pipeline (including the preamble) is wrapped in a fail-safe `try/except` that logs and lets TD finish writing the `.toe`.

### First-launch palette scan

- **A fresh project on a new TD build no longer floods the textport with alarming (but harmless) errors.** The shipped bootstrap catalog now covers build `099.2025.32820` (projects on that build skip the live scan); the scan blocklist gained dependency-requiring families (`tdAbletonPackage`, `ableton*`, `resources`, `world`, `system`); and a first-launch banner frames any remaining scan errors as expected and one-time.

### Window header / UI

- **Fix: top-level manager rows show the expand/collapse glyph.** Depth-0 rows with children now get one base indent level so the +/- affordance renders at the same offset depth-1 rows use.
- **Fix: removed a duplicate UTF-8 BOM** that an edit had introduced at the top of `WindowHeaderExt.py` — a second `U+FEFF` before the docstring could raise `SyntaxError` when the extension reinitializes.

### Docs

- **New AI-first Quickstart page** (`docs/quickstart.md`) — install → drag in → Enable Envoy → connect your AI client, with per-client steps and troubleshooting; linked from Home, the nav, and the web landing page.
- **New "Excluding a COMP from TDN" section** documenting the exclude tag.
- **Reconciled the Envoy MCP tool count to 48** across all current-facing surfaces (the 4 bridge meta-tools are counted separately).
- **POP skill corrections** — POP = "Point Operators"; File In POP (meshes) and Point File In POP (point clouds) are distinct operators. Template twins synced.
- **Rewrote the landing-page meta descriptions** to experience-first copy.
- **Fix: de-mapped dev-only `.claude` files no longer self-delete on an AI-Project-Root flip** (`release-commits.md`, `multi-instance/SKILL.md`) — markers stripped so cleanup treats them as hand-maintained dev files.

### Tests & review

- **57 test suites / 1,401 tests, all passing.** New and updated coverage across the changeset: 21 tests for `tdn_exclude` (including nested-under-normal now *preserved* rather than lost), the TDN fingerprint/dirty-detection suite (the no-churn-on-live-expressions guarantee, `DirtyCount` strategy-awareness, and clean-clear-on-revert), the widened issue #21 cell-read surface, the Envoy startup-status contract, and the save-time content-safety dialog.
- **Final regression-review pass before release.** A 7-angle review of the branch (line-by-line, removed-behavior, cross-file, plus reuse/simplification/efficiency/altitude) surfaced and fixed: the live-expression dirty churn, the nested-exclude data-loss path, the always-dirty `DirtyCount` for TDN COMPs, the stuck dirty flag, the duplicate BOM, and the double fingerprint sweep. Each fix carries a regression test.
- **Fresh-install smoke test.** The release `.tox` was loaded into a blank project in a separate TD instance and verified: status `Enabled`, no script errors, all three extensions loaded, Envoy bound, externalizations schema intact, and the header status prefix present.

---

## v5.0.414

Third value `Custom` for `AI Project Root` (follow-up to Ten0's feedback on issue #19) — lets the user pick any directory as the AI/MCP config root, not just git root or `.toe` folder. Useful for monorepos where multiple `.toe` files share a parent directory and should converge on one set of `AGENTS.md` / `.claude/` / `.mcp.json` / `.embody/` instead of duplicating per project. Plus two defense fixes against a previously-unobserved class of test interference: tests with exhausted or missing seeded responses no longer open real modal dialogs that freeze TD; `Verify()` can no longer queue multiple Envoy opt-in prompts in quick succession.

- **Feature: `AI Project Root = Custom`** — new menu option on the Envoy page, paired with a new `AI Project Root (Custom)` Folder parameter. The custom path can be absolute (e.g. `/Users/foo/touchdesigner/`) or relative to the `.toe` directory (e.g. `../` for "one level up"). The Folder parameter is greyed out (`enable=False`) unless the menu is set to `Custom`. Flipping the menu, or changing the custom path while in Custom mode, migrates Embody state and AI config to the new location — same atomic move + marker-aware cleanup as the gitroot↔projectfolder flip. For Ten0's monorepo use case (#19 follow-up), each `.toe` in the same parent dir just sets the same relative path and they all share `AGENTS.md` / `.mcp.json` / `.embody/envoy.json` — which lets the multi-instance MCP feature work naturally across sibling projects.
- **Fix: `_findSettingsFile` walk-up fallback handles the Custom mode chicken-and-egg.** At TD launch, `Aiprojectroot` sits at its baked-in default before settings are restored. For gitroot↔projectfolder, the alternate root is computable without reading `config.json` (just walk to `.git` or use `project.folder`). For Custom, the alternate path lives *inside* `config.json` — chicken-and-egg. Solution: after checking the predefined alternates, walk up from `project.folder` looking for any `.embody/config.json` directory. The user's saved custom location is found regardless of what the baked-in default says, so settings restore survives across restarts even when the user closed TD without saving the `.toe` after a flip.
- **Fix: tests can no longer open real modal dialogs that freeze TD.** `_messageBox` previously fell back to `ui.messageBox(...)` when the test framework's seeded responses were exhausted or missing — a single-int seeded response is consumed on first use, so a test that triggered N dialogs got one auto-answer and N-1 real modal dialogs stacking up after the test finished, freezing TD with no way out short of force-quit. New behavior: when `_smoke_test_responses` is set in storage (test mode), missing or exhausted responses return `-1` and log a WARNING instead of opening a modal. Belt-and-suspenders with the next fix.
- **Fix: `Verify()` no longer re-queues the Envoy opt-in prompt while one is already pending.** Tests that ran multiple `Verify()` cycles in succession (e.g. `test_custom_parameters`'s Disable/Enable suite) would each hit the `else` branch and set `_pending_envoy_prompt = True`, stacking N prompts even with only one auto-response seeded. Gating on `getattr(self, '_pending_envoy_prompt', False)` makes the flag idempotent — at most one prompt queued at a time, regardless of how many times `Verify()` runs.

---

## v5.0.413

Two independent bodies of work bundled into one build. First: issue #20 fix — parent `.tdn` files no longer embed the contents of TOX-externalized child COMPs (mirrors the existing `tdn_ref` pattern with a new `tox_ref`; TDN format bumped to v1.4). Plus the round-trip restore path, backward-compat strip for pre-v1.4 files, and a substantial Envoy-toggle frame-drop fix surfaced while diagnosing the broader change. Second: a new `AI Project Root` parameter for monorepo TouchDesigner projects (and the underlying fix for issue #19 — `Path.home()` length comparison broke on Windows non-home drives), with a cluster of safety improvements found by a 10-agent cross-AI review of the change.

### TDN `tox_ref` and Envoy toggle perf

- **Fix: issue #20 — parent `.tdn` no longer duplicates TOX-externalized child contents.** When a parent COMP was exported as TDN and one of its children was externalized via the TOX strategy, the parent's `.tdn` snapshot recursed *into* the child and re-emitted the child's full subtree — including grandchildren types that then polluted the parent's `type_defaults` (e.g. `containerCOMP`, `outCHOP`, `parameterCHOP` from sliders bloating the parent file, defeating the entire point of TOX externalization). The exporter already handled this for nested **TDN** children (via `tdn_ref` since v1.2), but the symmetrical TOX case was missing — the asymmetric behavior had no design rationale, just an unwritten gap. New `_hasTOXTag` / `_resolveTOXRef` / `_getTOXExternalizedPaths` helpers in `TDNExt` mirror the existing TDN trio; `_exportSingleOp` writes a `tox_ref` pointer for TOX-tagged children and skips recursion; `_collectAllPaths` (async export) skips their subtrees as well. Both the metadata branch and the children-recursion branch were moved outside the `recurse=True` gate so async modular exports also emit `tdn_ref` / `tox_ref` (previously a latent bug — async produced shell-only children with no pointer at all). The TDN format version bumped to `1.4` and the schema (`docs/tdn.schema.json`) gained a `tox_ref` property; existing v1.3-and-earlier files continue to work via the externalizations table.
- **Fix: round-trip restore for `tox_ref` children.** `_createOps` only recognized `tdn_ref` for shell-only COMP creation — a `tox_ref` entry would create an empty COMP with no `externaltox` parameter (`externaltox` is in `SKIP_PARAMS`, so the exporter strips it). Combined with `ReconstructTDNComps`'s `clear_first=True` destroying any TOX child that `RestoreTOXComps` had just rebuilt at frame 45, the round-trip was broken: `.tox` content was loaded, then immediately wiped, with nothing to refill it until the next project open. New Phase 8.5 `_restoreTOXShells` walks the imported tree, finds any shell carrying a `_pending_tox_restore` storage marker (set by `_createOps`), sets `externaltox` from that marker, and calls `_reloadTox` to force TD to re-read the `.tox`. Runtime imports (e.g. `import_network` via MCP) and project-open reconstruction both now restore the `.tox` content immediately, with no second-save dance required.
- **Fix: pre-v1.4 `.tdn` files with embedded TOX children import cleanly.** New `_stripNestedTOXChildren` mirrors `_stripNestedTDNChildren` — consulted on import to empty out `children` arrays for any path matching a TOX entry in the externalizations table. Otherwise pre-fix files would re-create the embedded grandchildren into the live network, then `RestoreTOXComps` would clobber them with the actual `.tox` content (or worse, the embedded shells would sit there if the table was somehow out of sync). The TDN-side strip already had `tdn_paths.discard(target_path)` to avoid stripping the COMP currently being imported; added the symmetric `tox_paths.discard(target_path)` to the new strip.
- **Fix: cross-validation parity for `tox_ref`.** Added `_validateTOXRefs` (mirror of `_validateTDNRefs`) that warns when a `tox_ref` points at a path missing from the externalizations table or at a `.tox` file missing on disk. Both validators run during every import.
- **Fix: Envoy toggle no longer drops ~108 frames per cycle.** Diagnosed via temporary `ENVOY-PERF-START` instrumentation while looking at the broader fix. `_findAvailablePort` was paying `time.sleep(0.1) × 15 = 1.5s` on the main thread whenever a preferred port (e.g. 9870) was held by *any* listener, including foreign zombie TD processes that aren't in our `.embody/envoy.json` registry — force-closing our own shutdown events and uvicorn handle does nothing for a foreign process, so the wait was guaranteed to expire pointlessly. Three layered changes: (a) `_forceCloseOldServer` now returns `bool` indicating whether it actually closed a live uvicorn server of ours (`sys._envoy_uvi_server` was set) — re-signaling stale shutdown events for already-exited threads is housekeeping, not a port-holding signal; (b) the server thread's `finally` block clears `sys._envoy_uvi_server` (guarded by an `is` identity check so a newer Start that already replaced the handle doesn't get clobbered) so a subsequent Start correctly sees "nothing of ours is holding the port"; (c) `_findAvailablePort` now branches: if `_port_registered_by_other(base_port)` is True (foreign live instance in our registry), jump straight to scanning the range with no wait; if force-close had nothing to close (foreign zombie / clean prior shutdown), also skip the wait; only wait when we genuinely have a stale uvicorn handle of ours to drain — and even then, capped at 500ms (5×100ms) instead of 1500ms (15×100ms). Measured impact on the user-toggle path: total Start time dropped from ~1797ms → ~346ms, `findAvailablePort` from ~1527ms → ~10ms.
- **Fix: doc/code drift in TDN spec.** Spec previously claimed the importer creates the TOX shell "with `externaltox` pre-set" — but `externaltox` is in `SKIP_PARAMS` (excluded from TDN export) and `_createOps` had no `tox_ref` handler, so the documented behavior was fictional. Now both the code does what the docs claim (via the new Phase 8.5) and the docs accurately describe the `_pending_tox_restore` storage marker mechanism. Also added a "if a COMP carries both TDN and TOX tags, TDN wins" note in `docs/embody/externalization.md` to document the previously-undocumented precedence (TDN's `elif` branch in `_exportSingleOp` runs first).
- **Tests: 6 new in `test_tdn_file_io` (test count 66 → 92)**: `test_tox_ref_written_on_export`, `test_tox_ref_absent_without_tag`, `test_tox_ref_absent_with_embed_all`, `test_tox_children_stripped_on_import`, `test_tox_type_defaults_not_polluted` (direct regression test for the issue #20 symptom — two sibling TOX children with identical internal structure no longer leak their grandchild types into the parent's `type_defaults`), `test_tox_ref_consumed_on_import` (verifies `_createOps` skips child creation for `tox_ref` entries).

### AI Project Root and issue #19

- **Feature: `AI Project Root` menu parameter** (`gitroot` default / `projectfolder`) on the Envoy page. Controls where Embody writes AI/MCP config (`AGENTS.md`, `CLAUDE.md`, `.claude/`, `.cursor/`, `.mcp.json`, `.embody/`). `gitroot` preserves prior behavior — config lives at the top of the git repo, which is what every AI tool expects when the whole repo is the workspace. `projectfolder` writes config next to the `.toe` instead — the right choice when your TouchDesigner project lives in a subdirectory of a larger repo and you open that subdirectory as your AI tool's workspace (e.g. `myrepo/touchdesigner/` opened as the Cursor or Claude Code root). Flipping the parameter migrates Embody's own state (`.embody/config.json`, `project.json`, palette catalogs, `.claude/settings.local.json`) to the new root and cleans up Embody-generated AI files at the old root. User-authored files (custom skills, hand-edited `CLAUDE.md`, other entries in `.mcp.json`) are preserved.
- **Fix: issue #19 — `Path.home()` comparison no longer bails before searching on non-home drives.** `_findProjectRoot` (EmbodyExt), `_findGitRoot`, and `_checkOrInitGitRepo` (EnvoyExt) all compared `len(parent_dir.parts) <= len(home_dir.parts)` without checking whether home was actually an ancestor of the project. On Windows with a project on `D:\` and home on `C:\`, both paths have the same part count so the guard triggered immediately and the `.git` walk-up never started. Subsequent runs after the first successful git pick failed to find the repo, duplicated `.mcp.json` config, and broke the MCP connection. The fix only applies the home-dir stop when home is genuinely an ancestor (covers the original intent: avoid finding a stray `.git` in `~/.dotfiles`) and falls through cleanly when `Path.home()` raises.
- **Fix: Envoy registry I/O now honors `AI Project Root`.** `_writeEnvoyConfig` already wrote `.embody/envoy.json` under `_findProjectRoot()`, but `_port_registered_by_other`, `RefreshRegistry`, and `_removeFromRegistry` still derived the path from cached `_git_root`. Under `projectfolder` mode, port-conflict detection would silently disable, refresh would write to the wrong file, and shutdown would leave stale entries in the registry. New `EnvoyExt._registryPath()` helper routes all three readers through `_findProjectRoot()` (defensive fallback to `_git_root` if the Embody extension isn't accessible).
- **Fix: cross-filesystem migrations are now atomic.** Plain `shutil.move` falls back to copy + delete across filesystems — a crash mid-copy leaves a partial destination while the source is gone. Palette catalog files (large, expensive to regenerate, not rebuilt from settings) were the worst exposure. New `_atomicMove` helper copies to a sibling tmp file, `os.replace`s atomically into place (single-filesystem rename), then unlinks the source. A failed copy never leaves a half-written destination.
- **Fix: settings restore now survives the `AI Project Root` chicken-and-egg.** On TD launch, `init()` doesn't touch `Aiprojectroot`, so the parameter sits at its baked-in default (usually `gitroot`) when `_restoreSettings` runs. If the user previously flipped to `projectfolder`, the saved `config.json` lives at the project folder, not git root — the canonical `_settingsPath()` would miss it and silently bail, reverting every persisted setting (including the `Aiprojectroot` value itself) on every restart. New `_findSettingsFile()` checks both candidate roots before declaring the file absent and logs which one it picked up.
- **Fix: half-migration orphans are quarantined.** If `_atomicMove` fails for `config.json` or `project.json` mid-flip, the source remains at the old root. The post-Pass-1 sweep renames any leftover critical file to `.json.orphan` so `_findSettingsFile`'s fallback doesn't pick up stale data on the next restart. The user can delete the `.orphan` file manually if no longer needed.
- **Fix: `.claude/settings.local.json` follows the user across flips.** The file has no marker comment (it's JSON merged with user-added MCP permissions), so the marker-aware cleanup left it stranded at the old root. Migration now explicitly moves it if the new root doesn't already have one; if both exist, a WARNING tells the user to merge manually rather than blindly clobbering either copy.
- **Fix: legacy artifacts from prior Embody versions are swept on flip.** `_cleanupOldRootFiles` now removes `.claude/envoy-bridge.py` (moved to `.embody/` in v4.x), root-level `.envoy.json` (moved to `.embody/envoy.json`), `.embody.json` (moved to `.embody/config.json`), and `.envoy-tools-cache.json` (moved to `.embody/`) at the old root — these would otherwise survive forever in long-lived installs and prevent `.claude/` from `rmdir`'ing cleanly.

---

## v5.0.407

Critical Windows-only crash fix introduced by the v5.0.402 registry GC: any path that re-registered the instance in `envoy.json` (Envoy toggle off→on, save, port change) silently terminated TouchDesigner with no Python traceback once the registry contained the running process's own PID. Root cause was Embody's `_isPidAlive(pid)` resting on `os.kill(pid, 0)` -- on Windows, CPython's posixmodule implements that as `OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid)` + `TerminateProcess(handle, sig)` for *all* `sig` values including 0, so the "liveness check" literally told the OS to kill the process being checked. Plus a palette-scan timeline guard, an `_verifyMcpImportable` fast-path that stops tearing down 82 `mcp.*` submodules every toggle, and a bridge-side filter for TD's CEF/Web Render helper subprocesses that were flooding the bridge log.

- **Fix: `_isPidAlive` no longer terminates the process it's checking on Windows.** CPython's `os.kill(pid, sig)` on Windows routes through `OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid)` + `TerminateProcess(handle, sig)` -- there is no `sig==0` special case for liveness checking. Embody's `_isPidAlive` (added with the registry GC in v5.0.402) had been built on `os.kill(pid, 0)`, so any time `_writeEnvoyConfig` iterated `instances` and the iteration hit the running TD's own PID (which it does any time the project has been saved with Envoy enabled and the GC pass runs on the existing entry), the list comprehension at line 4774 called `TerminateProcess(self_handle, 0)` and TD exited with code 0. Fingerprint: silent process death, no traceback, only on Windows, only when the registry holds an entry whose `td_pid` matches the running process, fresh projects unaffected because the registry hadn't accumulated. Confirmed end-to-end on the affected user's machine via a monkey-patched verify script: registry had exactly one row keyed to his own PID, the script's `about_to_run` for that PID was the last entry written before TD vanished mid-call. Replaced with the safe `OpenProcess(SYNCHRONIZE)` pattern via ctypes (mirrors `envoy_bridge.is_process_alive`, in production indefinitely); SYNCHRONIZE access does not include termination rights -- the worst this can do is wait on a handle. Also defends against the secondary failure mode (`OSError: [WinError 87]` → `SystemError: <class 'OSError'> returned a result with an exception set`) that surfaces for registry entries where `OpenProcess` returns `INVALID_HANDLE_VALUE` instead of NULL; that path's WinError 87 leaves the interpreter thread state inconsistent and corrupts subsequent ticks. Strict input gate (`isinstance(pid, int) and pid > 0`) rejects None/string/bool/negative without a syscall; POSIX path catches `OverflowError`/`ValueError` so corrupted-registry giants don't propagate. Also fixed the duplicate `_os.kill(other_pid, 0)` inside `_findAvailablePort._port_registered_by_other` -- same bug, would have silently killed any foreign live TD whose registry entry shared the port; rewired to call the shared safe `_isPidAlive` helper.
- **Fix: palette-scan no longer pauses the timeline.** `CatalogManager._processPaletteChunk` does `wrapper.loadTox()` on every shipped palette `.tox` to map names to types, which runs each component's init code live. At least one shipped component on TD 2025.32820 (likely the refactored `Palette:logger v2.7.0`, which now "parents with default TDAppLogger and internal loggers" on init, or the changed `Palette:moviePlayer`/`Palette:movieEngine`) mutates *global* timeline state on init -- the existing `_PALETTE_SCAN_BLOCKLIST` only covered `tdvr`/`autoui`. `_startPaletteScan` now snapshots `me.time.play`, `me.time.rate`, `project.cookRate`, `project.realTime` before queueing the scan; `_processPaletteChunk` restores any of those that changed after every chunk (so a misbehaving component can't leave the timeline paused for the rest of the scan either); `_finalizePaletteScan` does a final restore. Behaviour-equivalent to expanding the blocklist component-by-component, without requiring us to identify the offender in every future TD build.
- **Fix: `_verifyMcpImportable` fast-path on the `sys.modules` teardown.** The v5.0.393 implementation cleared every `mcp.*` from `sys.modules` and re-imported `mcp.server` on *every* `Start()` -- about 82 submodules per cycle, wasted work that runs on every Envoy toggle. (Initial hypothesis was that the teardown was *the* crash culprit via pydantic_core re-registration; direct repro disproved it, but the cleanup is still correct.) Now short-circuits when `mcp.server in sys.modules` -- a previous Start in this session already imported it. The `del`/re-import branch remains for genuine first imports and recovery from prior failed imports (covered by the dedicated regression test `test_A03_only_mcp_in_modules_not_mcp_server_does_not_short_circuit`).
- **Fix: bridge `find_all_td_pids()` filters CEF / Web Render helpers.** `pgrep -f TouchDesigner` matches `TouchDesigner Web Render.app/Contents/MacOS/TouchDesigner` (and the CEF GPU/renderer subprocesses TD spawns under it) because they share the executable basename. CEF recycles those children every few seconds, flooding the bridge log with phantom `New TD process detected` / `TD process exited` entries on a ~10s cadence (observed in one session: 214,751 such entries across a 29 MB log) and triggering needless config re-reads every cycle. Added `_process_cmdline(pid)` helper and `_is_td_helper_process(pid)` that checks for `"Web Render"` and `"--type="` markers in the cmdline; `find_all_td_pids` filters them out alongside the existing bridge-self filter. Refactored `_is_bridge_process` to share the cmdline helper. Three identical copies updated in lock-step: dev source (`dev/embody/envoy_bridge.py`), deployed (`.embody/envoy-bridge.py`), and the textDAT template (`dev/embody/Embody/templates/text_envoy_bridge.py`).
- **Fix: OS label disambiguates Windows 11 from Windows 10.** TD's `app.osVersion` reports `"10"` on both Windows 10 and Windows 11 because they share NT kernel version 10.0; the only reliable discriminator is the build number (≥22000 = Windows 11). `EmbodyExt._osLabel()` (called from `execute.py:init()`) now probes `sys.getwindowsversion().build` and corrects the label so startup logs and `get_td_info` no longer mis-label Win 11 machines. Pure `_resolveOsLabel(os_name, os_version, win_build)` is isolated from TD globals for testability; macOS / genuine Win 10 / non-Windows pass through unchanged.
- **Fix: `execute_src_ctrl.py` reads/writes `README.md` as UTF-8.** README contains emoji; the locale-default codec crashed on Windows under non-UTF-8 console code pages. Pinned both the read and write to `encoding='utf-8'`.
- **Tests: 8 new in `test_envoy_registry` (`TestIsPidAliveSafety`)**: `_isPidAlive` contract -- zero/None/negative/string/bool/oversized PIDs all return False without raising, own PID returns True, definitely-dead high PID returns False. Pins the safe contract against the SystemError class of regression.
- **Tests: 6 new in `test_catalog_palette_scan` (`TestCatalogPaletteScanTimelineGuard`)**: `_snapshotTimeState`/`_restoreTimeState` round-trip pause/cookRate/realTime, restore is a no-op without a snapshot, restore is a no-op when nothing changed, snapshot captures every tracked key.
- **Tests: 3 new in `test_envoy_setup_environment` (`TestVerifyMcpImportableFastPath`)**: fast-path returns True when `mcp.server` is loaded, sentinel `mcp`/`mcp.server`/`mcp.types` module objects are NOT replaced on the fast path (the regression guard), half-loaded `mcp` parent without `mcp.server` does NOT short-circuit so recovery still runs.
- **Tests: 2 new in `test_envoy_bridge` (`TestBridgeProcessManagement`)**: `test_find_all_td_pids_filters_helper_processes` (Web Render + `--type=` cmdlines are skipped, real TD pid survives), `test_is_td_helper_process_markers` (marker detection directly).
- **Tests: new `test_os_label.py`**: covers `_resolveOsLabel` -- Windows 11 disambiguation by build ≥22000, Windows 10 pass-through, macOS pass-through, missing `win_build` defaulting to the raw label.
- **Diagnostics: three dev-only bisect helpers in `dev/embody/diagnostics/`** -- `diagnose_envoy_toggle_crash.py` (breaks down `Start()` step by step), `diagnose_envoy_toggle_crash_v2.py` (breaks down `_configureMCPClient` body step by step), `verify_ispidalive_fix.py` (installs the patched `_isPidAlive` as a monkey-patch in a running TD and exercises both old and new code paths against the live registry). All use a flush-before-each-step JSON write so a silent process death leaves the diagnosis intact on disk; used inline during root-cause analysis and kept for future debugging of this class of failure.
- **Test file count: 50 → 53.** New files: `test_catalog_palette_scan.py`, `test_envoy_setup_environment.py`, `test_os_label.py`.

## v5.0.403

Hotfix for a one-line typo in the v5.0.402 rename-detection backstop.

- **Fix: `EmbodyExt.Update()` rename-detect uses `self.my`, not `self.ownerComp`**: The new rename-detection block added in v5.0.402 referenced `self.ownerComp.ext.Envoy.RefreshRegistry()`. `EmbodyExt` stores its owner COMP as `self.my` (line 82: `self.my = ownerComp`); only `EnvoyExt` uses `self.ownerComp`. I copied the pattern from EnvoyExt without verifying. Result: every `Update()` tick during a v5.0.402 save threw `'EmbodyExt' object has no attribute 'ownerComp'`, the warning got logged but the rename-detect path never actually fired -- the registry wouldn't walk forward on save. Caught immediately on first fresh-session check by inspecting `dev/logs/Embody-5.402.toe_*.log`. The Layer 2 walk-forward in the bridge masked the user-visible symptom (lookups still resolved to the new .toe), but the registry would have stayed perpetually keyed to the previous version. One-character fix; unit-test path was unaffected since the test code paths don't exercise this property reference.

## v5.0.402

Three closely-related fixes for the registry that landed during follow-up testing of v5.0.401: dead-PID rows now garbage-collect on every write (catching the accumulation that hard-kills/force-quits/crashes leave behind), `Update()` watches for `.toe` basename changes as a backstop for execute.py's postSave hook in case it didn't reload, and the bridge's `launch_td` guard scans every alive instance by PID instead of relying on the (potentially stale) registry key.

- **Fix: registry GC -- dead-PID rows pruned on every write**: Embody only deregisters from `envoy.json` on graceful shutdown (`Stop()`/`onDestroyTD`). Hard kills, force-quits, OS crashes, and Cmd+Q-without-Envoy-stop all leave entries behind that accumulate across sessions -- a long-running developer would routinely see 20-50 dead rows. Reported in this session: a registry with 28 entries, 27 dead. `_writeEnvoyConfig` now scans `instances` and removes any row whose `td_pid` is no longer alive (uses `_isPidAlive` -- `os.kill(pid, 0)`, no syscall to verify it's actually a TD process, but PID recycling collisions self-correct on the next write since the new owner re-registers). Runs on every registry write -- Envoy startup, save-time `RefreshRegistry`, and the new `Update()` rename-detect path. The registry stays bounded automatically. Verified: a 28-row registry collapsed to 1 row on the first post-fix save.
- **Fix: `EmbodyExt.Update()` watches `project.name` and triggers `RefreshRegistry`**: `execute.py`'s `onProjectPostSave` already calls `RefreshRegistry`, but `execute.py` is a project-lifecycle script and its in-process reload behavior across edit-on-disk sessions is unreliable. The v5.0.401 fix could miss saves whenever the running TD held a stale copy of `execute.py`. New defensive backstop: `Update()` (which runs on every Refresh pulse and parameter change) caches `_last_toe_name` and compares to `project.name` each tick. On mismatch, it sets the new name and calls `Envoy.RefreshRegistry()`. `EmbodyExt` auto-reloads on source change, so this hook is reliable in a way that depending on `execute.py` reload isn't. Idempotent -- `_writeEnvoyConfig` short-circuits when the registry is already current.
- **Fix: bridge `launch_td` guard is PID-aware, not just key-aware**: The v5.0.401 walk-forward in `resolve_toe_path` interacted badly with the v5.0.399 instance-specific guard. When the registry has a stale key (e.g. registered as `Embody-5.400` even though the live `.toe` is now `.401`), the walk-forward correctly resolved the target to `Embody-5.401`, then the guard looked up `instances["Embody-5.401"]`, missed (because the row is still keyed under `.400`), and let the launch proceed -- spawning a duplicate TD pointing at the same `.toe`. Triggered live during v5.0.401 verification. Fixed in `handle_launch_td` by adding a slow-path PID-aware scan after the fast-path key lookup: iterate every instance, skip dead PIDs, walk-forward each registered `toe_path`, and refuse if any resolves to the same target. Names the stale key in the error message so the user understands what to `switch_instance` to. Catches the stale-key edge case automatically.
- **Tests: 4 new in `test_envoy_registry` (`TestRegistryDeadPidGC`)**: `test_dead_rows_pruned_on_write` (28-row tempdir registry collapses to 1 after `_writeEnvoyConfig`), `test_live_foreign_row_preserved` (foreign live PID stays, dead rows go), constructed against synthetic envoy.json files in a tempdir + injected `_isPidAlive` predicate so the test runs deterministically without relying on actual machine state.
- **Tests: 2 new in `test_envoy_bridge` (`TestBridgeLaunchTd*`)**: `test_launch_td_pid_aware_guard_catches_stale_key` (registers `Project-1.400` with our PID, walks forward to `Project-1.401.toe`, refuses with the .400 key in the message), `test_launch_td_pid_aware_guard_ignores_dead_pids` (registered toe walks forward to target but PID is dead -> fall through past the guard, fail on the executable check). Test file count stays at 50.

## v5.0.401

`envoy.json` registry now walks forward across TD's save-time .toe version bump (`Foo-5.398.toe` -> `Foo-5.399.toe`), so the bridge keeps tracking the live instance instead of orphaning a stale entry. Two-layer fix: Embody re-registers under the new basename on save (proactive), and the bridge defensively iterates up to the highest-versioned sibling when an active entry's `toe_path` no longer exists. Plus a hotfix for the post-save call's incorrect `self.port` reference (caught in the same session: the v5.0.400 save itself surfaced the bug).

- **Feature: Embody-side rename walk-forward in the instance registry**: TD's `project.save()` increments the trailing numeric segment of the .toe filename. The save handler then needs to update `envoy.json` so the bridge can keep talking to the same TD process under its new basename. Two-part change in `EnvoyExt._instanceKey` and `_writeEnvoyConfig`. The key computation now distinguishes "same PID, same toe_path" (idempotent re-register, returns the existing key) from "same PID, different toe_path" (rename in progress, returns the new basename so the caller can prune). The writer side runs an explicit prune pass after computing the key -- any other rows belonging to the current PID under different keys are deleted, so the registry walks forward instead of accumulating dead aliases. `RefreshRegistry()` is a new public method that re-registers from the live process state; called from `onProjectPostSave` in `execute.py` so the registry gets rewritten regardless of whether Envoy restarts (it does in Full mode but not Off/Export). Caught by an in-session repro: a save renamed `Embody-5.398.toe` -> `Embody-5.399.toe` but the registry stayed pointed at `.398`, leaving the bridge unable to reach the running TD on the next session restart. Now: registry follows the rename, stale row pruned in the same write.
- **Feature: bridge-side defensive walk-forward in `resolve_toe_path`**: Layer 2 of the same fix, in case Layer 1 didn't fire (manual rename outside TD, save-as-version-up, Embody disabled at save time, etc.). New helper `find_latest_versioned_toe` strips the trailing `<digits>.toe` from a missing path to derive a prefix, scans the directory for siblings, and returns the path with the highest extant numeric suffix. `resolve_toe_path` now reads from `instances[active]` first (the multi-instance format -- previous flat-format-only behavior would silently return None for any modern config), falls back to legacy top-level `toe_path`, and walks the result through `find_latest_versioned_toe` so a stale registry entry still resolves to a usable file. The bridge does NOT rewrite `envoy.json` from this path -- registry mutation stays Embody's responsibility; the bridge just uses the corrected file in-memory and logs a warning.
- **Fix: `RefreshRegistry()` no longer crashes with `'EnvoyExt' object has no attribute 'port'`**: Initial implementation read `self.port`, which is an attribute of the worker-thread `EnvoyMCPServer`, not the main-thread `EnvoyExt`. The actual runtime port isn't retained on the extension (it's a local in `Start()`), so `RefreshRegistry()` now reads it from `envoy.json` by looking up the row whose `td_pid` matches `os.getpid()`. Single source of truth (the registry itself) -- no instance attribute to keep in sync. The bug was harmless on Full-mode saves because Envoy restarts after the strip and re-registers correctly anyway, but it'd have been broken silently on Off/Export-mode saves where there's no restart. Caught immediately on the first save by the user.
- **Tests: 7 new in `test_envoy_registry`**: covers `_instanceKey` directly. `test_basename_used_when_registry_empty`, `test_existing_key_reused_when_toe_path_unchanged` (idempotence), `test_walks_forward_when_toe_path_changes_for_same_pid` (the rename case), `test_reclaims_own_basename_collision` (PID's own row at the new name), `test_appends_suffix_for_live_foreign_pid_collision` (foreign live PID -> -2 suffix), `test_reclaims_dead_basename` (stale row reclaimed), `test_old_pid_entry_not_reused_when_toe_changed` (sanity).
- **Tests: 13 new in `test_envoy_bridge`**: 7 in `TestBridgeVersionIteration` covering `find_latest_versioned_toe` (returns input on existence, walks forward, picks highest among many, no-siblings, unrelated files ignored, non-digit suffix no-walk, missing directory) and 5 in `TestBridgeResolveToePath` covering `resolve_toe_path` (multi-instance format, multi-instance walk-forward, legacy flat format, empty config, missing-active key). Plus `test_launch_td_unrelated_td_running` proving the v5.0.399 instance-specific guard correctly allows launching alongside an unrelated TD project.
- **Tests: 1 updated**: `test_launch_td_already_running` rewritten for the v5.0.399 instance-specific guard. The old assertion looked for "already running" against any TD; new assertion sets up a registry entry where the target instance's PID matches `os.getpid()` and verifies the error names the specific instance, not a generic "TouchDesigner is already running". Test file count goes from 49 -> 50 (`test_envoy_registry.py` is new).

## v5.0.399

New `edit_dat_content` MCP tool for token-efficient surgical edits to text DATs, plus a bridge multi-launch fix so Envoy can launch a TD instance alongside an unrelated TD project. Reported as token-cost feedback by Jeff.

- **Feature: `edit_dat_content` MCP tool — surgical text edits without round-trip cost**: `set_dat_content` is full-replace by design — even a two-line edit in a 500-line DAT pays for the entire DAT's content in the tool call. Reported by Jeff in the Embody chat: typical agent edits were adding ~2k tokens for trivial changes inside large DATs. The new `edit_dat_content(op_path, old_string, new_string, replace_all=False, confirm_wipe=False)` tool mirrors Claude Code's Edit tool exactly: `old_string` must appear exactly once by default, otherwise the caller widens it with surrounding context for uniqueness or passes `replace_all=True`. Only the changed substring crosses the wire, so a 2-line edit in a 500-line DAT now sends ~2 lines instead of ~500. Text DATs only — table DATs go through `set_dat_content(rows=...)` since string matching across cells is a different beast. Refuses empty `old_string` (would match every position), refuses identical `old_string`/`new_string` (no-op), and reuses the v5.0.397 wipe guardrail: edits that would leave the DAT empty require `confirm_wipe=True`. Not-found errors include diagnostics (DAT length, row count, case-insensitive hint) so the agent can self-correct without a second `get_dat_content` round-trip. `set_dat_content`'s docstring now points users to the new tool for partial edits.
- **Feature: bridge multi-launch — Envoy can launch alongside unrelated TouchDesigner projects**: `handle_launch_td` previously refused to launch if *any* TD process was running, even an unrelated project on a different `.toe`. The instance registry has supported multi-instance since bridge v2, so the blanket guard was overly conservative. Replaced with an instance-specific check: only refuses if the *target* `.toe`'s registered PID is alive (suggests `switch_instance` instead). Other TDs are now passed through cleanly. The macOS launch path also gained the `-n` flag (`open -n -a TouchDesigner.app file.toe`) — without it, LaunchServices reuses an existing TD window and spawns no new process, which silently broke multi-instance. The PID-detection step after spawn now diffs `find_all_td_pids()` against a pre-launch snapshot instead of returning `pids[0]`, so the bridge correctly identifies the new TD's PID even with multiple TDs running. `launch_td()` (the helper) gained an optional `existing_pids` parameter for this; `handle_launch_td` snapshots before delegating.
- **Test debt: `test_set_dat_content_clear` updated for v5.0.397 wipe guard**: The wipe guardrail shipped in v5.0.397 added a `confirm_wipe=True` requirement for `clear=True` calls with no replacement content, but the existing `test_set_dat_content_clear` regression test was missed in that release's test sweep — it's been failing since v5.0.397 because it called `clear=True` without the new flag. One-line fix to add `confirm_wipe=True`. Caught while running the new `edit_dat_content` suite.
- **Tests: 11 new tests in `test_mcp_dat_content`**: `test_edit_dat_content_basic` (find-and-replace with unique match), `test_edit_dat_content_requires_unique_match` (refuses 3-occurrence match by default with explicit count in error), `test_edit_dat_content_replace_all` (opt-in replaces every occurrence with replacement count returned), `test_edit_dat_content_not_found` / `test_edit_dat_content_case_insensitive_hint` (diagnostic when only case differs), `test_edit_dat_content_empty_old_string`, `test_edit_dat_content_identical_strings`, `test_edit_dat_content_rejects_table_dat` (text-only enforcement), `test_edit_dat_content_nonexistent`, `test_edit_dat_content_wipe_guard` (refuses if result would be empty), and `test_edit_dat_content_wipe_confirmed` (accepts wipe with explicit flag, asserts content actually emptied). All 20 tests in the suite now green.
- **Docs: `edit_dat_content` listed in tool reference**: `docs/envoy/tools-reference.md` and `docs/envoy/index.md` add the new tool entry. `.claude/skills/mcp-tools-reference/SKILL.md` (and matching template) get the full row with the partial-edit guidance and uniqueness/replace_all semantics. `set_dat_content`'s row updated to recommend `edit_dat_content` for partial edits and reserves itself for tables, full rewrites, and intentional wipes. `.claude/rules/skill-prerequisites.md`, the `td-api-reference` SKILL description (and template), and `text_claude.md`'s tool-loading checklist all add `edit_dat_content` alongside `execute_python` and `set_dat_content` since editing DAT contents may involve writing TD Python.

## v5.0.398

Hotfix for a latent race condition that silently broke the first-install dialog flow on fresh-project drops. The bug was older than v5.0.397 — surfaced when a user finally tested a fresh-drop on a machine without a cached catalog.

- **Fix: `Update()` no longer races with `EnsureCatalogs()` on fresh-drop**: When a user drops the release `.tox` into a brand-new project that has no `.embody/catalog_<build>.json` cached, `CatalogManagerExt.EnsureCatalogs()` kicks off a background scan and sets `Embody.par.Status = 'Scanning defaults (X/N)'` to show progress. That scan runs concurrent with the post-onCreate `Verify → UpdateHandler → Update → _promptEnvoy` chain. The chain sets `_pending_envoy_prompt = True` in `Verify`, then `Update` was supposed to consume it and schedule `_promptEnvoy`. But `Update` had `if self.my.par.Status != 'Enabled': return` — too strict. When the catalog scan won the frame race (which it usually did, because the scan starts at +45 and Update at +44 from onCreate, well within scheduler jitter), Status was already `'Scanning defaults (...)'`, Update returned early, the prompt flag was never consumed, and the Envoy opt-in dialog never appeared. User never got the chance to enable Envoy or initialize git — `.embody/` ended up containing only the cached catalog JSON. Latent for many releases. Fixed by changing both gates (`Update()` and `ReconcileMetadata()`) from `Status != 'Enabled'` to `Status == 'Disabled'`. Embody is functionally enabled during scanning/testing — those transient Status values must NOT block normal operation. Reported by a fresh-drop test on Windows after the v5.0.397 release.
- **Tests: 2 new regression tests in `test_smoke_release`**: `test_update_consumes_pending_prompt_during_catalog_scan` directly reproduces the race (sets Status='Scanning defaults', sets `_pending_envoy_prompt=True`, calls Update, asserts the flag was consumed). `test_update_skips_only_when_disabled` verifies the new contract — Update runs for every transient Status value (`Scanning defaults`, `Scanning palette`, `Testing`) and only short-circuits when Status is explicitly `'Disabled'`. Both fail without the fix.

## v5.0.397

Three independent improvements bundled together: a wipe guardrail on the `set_dat_content` MCP tool to prevent silent destruction of user content from malformed agent calls, a TDN at-risk filter that excludes TD-managed read-only DAT types from the save-time content-loss warning, and a deterministic settings serialization fix that closes issue #18. Plus a substantial test-debt cleanup that brings the previously-failing legacy tests back to green.

- **Feature: `confirm_wipe` guardrail on `set_dat_content`**: The MCP tool is full-replace by design, but agents occasionally call it with empty `text=""`, empty `rows=[]`, or `clear=True` with no replacement content — silently destroying everything in the DAT. Reported by a user whose agent twice rebuilt the same DAT after wiping it without realizing. The handler now refuses any call whose result would be an empty DAT unless the caller passes `confirm_wipe=True`. The check inspects the *resulting* state, not just inputs, so legitimate atomic-replace calls (`clear=True, text="hello"`) still work without the flag. Error message names the override and points back to `get_dat_content` as the proper read-modify-write workflow. A second guard refuses no-content calls (`text=None, rows=None, clear=False`) — same failure shape (silent confused success), refused the same way. Tests cover empty-text, empty-rows, the no-op case, atomic-replace pass-through, single-empty-row not-a-wipe, whitespace not-a-wipe, no-partial-mutation guarantee on rejection, and the explicit `confirm_wipe=True` override path.
- **Feature: TDN at-risk dialog skips TD-managed DAT types**: The save-time "TDN Content at Risk" warning previously flagged every non-empty unexternalized DAT inside a TDN-strategy COMP, including TD-generated read-only DATs (Info DAT, WebRTC DAT, Folder DAT, Monitors DAT, device-discovery DATs, Error/Perform/Examine, etc.) whose content TD regenerates on cook. Users couldn't act on these warnings — the content isn't theirs to preserve. New `_TD_MANAGED_DAT_TYPES` denylist excludes the 19 known read-only generator types from the at-risk scan. Callback DATs (executeDAT, chopExecuteDAT, datExecuteDAT, panelExecuteDAT, parameterExecuteDAT, etc.) are intentionally NOT in the set — those hold user-authored Python and losing them silently is exactly what the warning exists to prevent. Reported by a user whose Moonshine projection-mapping project was getting noise from `deform_info`, `keystone_info`, and `webrtc_dat` operators on every save.
- **Fix: `.embody/config.json` is now byte-stable across saves (issue #18)**: `_PERSISTED_PARAMS` is a frozenset and Python's per-process hash randomization gave each TD session a different iteration order. `_saveSettings` used that order to populate the params dict and `json.dumps` preserved insertion order, so the file got a different (but valid) key ordering every session — producing a noisy diff on every `git status` even when no settings changed. Two surgical changes inside `_saveSettings`: iterate `sorted(self._PERSISTED_PARAMS)` and pass `sort_keys=True` to `json.dumps`. The frozenset is unchanged so O(1) membership checks elsewhere still work. First commit after the fix shows one-time noise as the on-disk file rewrites in sorted order; after that, stable. Reported by chrsmlls333.
- **Test debt: 28 stale `.txt` test files removed**: Pre-existing duplicate test files alongside their `.py` counterparts in `dev/embody/unit_tests/`, leftovers from an earlier externalization format. Not referenced by any DAT or `externalizations.tsv` entry, but the test runner discovered both `.py` AND `.txt` from disk and ran every duplicated suite twice — bloating run times and obscuring real failures behind double-counted noise. Suite count for the full run drops from 103 discovered classes to 74 (the actual file count). No coverage lost; every `.txt` was byte-identical to its `.py` (or stale).
- **Test debt: `test_ancestor_rename` tearDown leak fixed (4 tests)**: All four "should succeed" assertions in `_handleAncestorRename` were failing intermittently because the test's `tearDown` cleaned `dev/embody/unit_tests/_test_ancestor` (a path nothing actually wrote to) instead of the real test-created prefix dirs (`dev/embody/retval/`, `tblupd/`, `tdntest/`, `cancel_test/`, `conflict/`, `phaseA/`, etc.). After a successful rename, the renamed-target dir was left on disk; the next run hit the (correct!) "Target directory already exists" guard in `_handleAncestorRename` and the test failed even though production code was working perfectly. New `tearDown` snapshots top-level dirs in `dev/embody/` at setUp and removes any new ones at teardown, plus rmtrees the workspace dir under the sandbox for the no-ext-folder test path. All 19 tests in the suite now pass on consecutive runs.
- **Test debt: 3 envoy_bridge stubs converted to real tests + 1 deleted**: `TestBridgeV2DeferredStubs` carried three `raise SkipTest('depends on bridge v2 step N')` placeholders left behind from when the bridge v2 features weren't shipped yet. All three features (local ping handler, `find_all_td_pids`, 3s initial probe + bridge-only fallback) have actually been live since v5.0.391 — the stubs were just stale TODOs that registered as ERRORs in the test runner. Replaced with two real tests for the local ping handler (request returns `{result: {}}`, notification produces no output) and four real tests for `find_all_td_pids` filtering (excludes own PID, excludes bridge processes, returns `[]` on `TimeoutExpired` / `FileNotFoundError` / pgrep no-match). Deleted the 3s-probe stub entirely — already covered by `test_tools_list_bridge_only_when_td_down` and `test_full_mcp_handshake_when_td_down`.
- **Test debt: 3 tdn_reconstruction palette tests aligned with current production contract**: `test_V03_palette_clone_flag_in_export`, `test_V07_clone_enablecloning_excluded_from_export`, and `test_V12_mixed_network_no_interference` were written against the old palette-detection model where native widget COMPs (`buttonCOMP`, `sliderCOMP`) cloned from `/sys/TDTox/defaultCOMPs/` were tagged with the `palette_clone` flag and had their children stripped from export. Production behavior was intentionally changed (commit `e759b89`) to exclude `defaultCOMPs/*` from palette-clone detection, so native widgets export as regular COMPs with full children — preserving any user customization inside the widget's internals. Tests rewritten to assert the new contract (no `palette_clone` flag, `children` exported, `clone` reference captured in per-op params, `enablecloning` correctly omitted as it matches its default). Section header docstring updated to describe the current model. The `palette_clone` flag remains reserved for true user palette clones from `/sys/TDBasicWidgets` and similar.
- **Tests: 23 new tests across 4 files**: 11 in `test_mcp_dat_content` (wipe guardrail), 3 in `test_tdn_safety_guards` (TD-managed DAT filter), 3 in `test_settings_persistence` (issue #18 regression coverage — byte-stability + key sorting), 6 in `test_envoy_bridge` (ping handler + find_all_td_pids), minus 1 deleted stub. Test file count goes from 48 → 49.
- **Docs: `set_dat_content` and at-risk filter behavior**: `.claude/skills/mcp-tools-reference/SKILL.md`, `dev/embody/Embody/templates/text_skill_mcp_tools_reference.md` (template counterpart), and `docs/envoy/tools-reference.md` updated to surface the new `confirm_wipe?` parameter with the wipe-guard contract. `docs/embody/externalization.md` Content Safety section now mentions the read-only DAT exclusion and the explicit callback-DAT inclusion, so users understand exactly which content types still trigger the warning.

## v5.0.393

Hardens Envoy's Python-environment bootstrap so silent failures surface a useful textport message instead of an inscrutable `No module named 'mcp.server'` traceback at server-start time. Fixes the user-visible half of issue #17 (the macOS Library Validation half was retracted by the reporter after verifying TouchDesigner.app ships with `com.apple.security.cs.disable-library-validation`, so prebuilt PyPI wheels load fine in-process).

- **Fix: bootstrap failures now abort `Start()` with an explicit error**: `EmbodyExt._setupEnvironment` previously returned `None` on every path including four silent-return failure paths (uv not findable, mcp version metadata unreadable, two `try/except` swallowed-error paths). `EnvoyExt.Start` called it fire-and-forget and proceeded to `_runServer` regardless, so any setup failure dropped the user into `RuntimeError: MCP server failed on port 9870: No module named 'mcp.server'` with no indication of why. `_setupEnvironment` now returns `bool`; each previously-silent return path logs an actionable message with platform-specific hints (e.g. "macOS GUI apps do not inherit shell PATH" when `shutil.which('uv')` comes up empty). `Start()` checks the return value, sets `Envoystatus = 'Error: Python environment not ready'`, logs `Aborting Envoy start -- See textport above for the underlying failure`, and returns before `_runServer` runs. The other call site at `_writeMCPConfig`'s venv-corruption recovery path is intentionally left ignoring the return — that path is already defensive with subsequent `is_file()` checks and exception fallback to system Python. Reported by Diego Chavez (issue #17).
- **Fix: final `import mcp.server` gate catches partial installs**: New `_verifyMcpImportable()` helper runs `importlib.import_module('mcp.server')` after the install step succeeds — a populated `site-packages` is necessary but not sufficient (a partial install or load-time failure such as a missing native dep would still leave the server unable to start). The helper drops any cached failed `mcp` / `mcp.*` entries from `sys.modules` before retrying so a TD-process import attempt that previously failed gets a clean re-evaluation. On failure it logs `Dependencies installed but mcp.server failed to import: <ImportError>. Inspect <site-packages> for partial installs and try deleting .venv/ to force a clean rebuild.` and returns False — feeds straight into the `Start()` gate. Catches the exact symptom Diego would have seen if his bootstrap had ever gotten past the empty-`site-packages` failure
- **Verified locally**: Ran the exact bootstrap subprocess sequence (`uv venv .venv --python <TD-bundled-python>` followed by `uv pip install "mcp>=1.26.0" "attrs<25" --python .venv/bin/python`) against TD's `Python.framework/Versions/3.11/bin/python3.11` — both succeed cleanly on macOS Sequoia / Apple Silicon, 20 packages land in `site-packages`, prebuilt wheels load fine inside TD's process. Confirmed `codesign -d --entitlements :- /Applications/TouchDesigner.app` shows `com.apple.security.cs.disable-library-validation` is set on the host process (the framework's standalone `python3.11` binary has no entitlements, which is why running `.venv/bin/python` from a terminal outside TD reports the dlopen Team-ID mismatch — irrelevant to actual Envoy startup since the wheels are loaded by TD's process, not by the standalone framework binary). Diego's "Problem 2" demand to default `--no-binary pydantic-core,cryptography` on macOS is therefore not just unnecessary but actively harmful — it would force every Mac user to install a Rust toolchain to compile from source for a problem that doesn't exist when wheels are loaded inside TD

## v5.0.392

Single critical fix for a Windows-only venv-destruction loop that bricked Envoy on machines where TouchDesigner's GUI-process stdin handle isn't duplicatable.

- **Fix: `subprocess.run` from inside TD no longer raises `[WinError 50]` on Windows**: Affected machines saw Embody's venv-bootstrap and verify-venv subprocess calls fail with `OSError: [WinError 50] The request is not supported`, traced to `subprocess._make_inheritable` calling `_winapi.DuplicateHandle` on the parent's `STD_INPUT_HANDLE` — TD's GUI process stdin handle is a console-buffer / non-duplicatable kernel object, so the duplicate fails before any child process is spawned. The verify-venv handler in `EnvoyExt._writeMCPConfig` treats `OSError` as "venv corrupt" and runs `shutil.rmtree(.venv)`, so on every TD restart the auto-recovery destroyed a perfectly healthy venv, ran the bootstrap (which also failed with WinError 50), and left the user with no `mcp` package and a crashing MCP server. Fixed by passing `stdin=subprocess.DEVNULL` on every `subprocess.run` in the bootstrap path (3 sites in `EmbodyExt._setupEnvironment` / `_findOrInstallUv`) and the verify-venv path (2 sites in `EnvoyExt._writeMCPConfig`) — routes through `NUL`, which is duplicatable. Confirmed via textport repro: `subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True)` raised WinError 50 on the affected machine; the same call with `stdin=subprocess.DEVNULL` returned `rc=0`. Reported by Jason Latta.

## v5.0.391

Three independent fixes shipped together: per-project TouchDesigner build pinning so the Envoy bridge can find the right install on a fresh clone, a thread-safety fix in the MCP update checker, and a 21-assertion cleanup of bridge tests that had been silently broken since the bridge v2 refactor.

- **Feature: `.embody/project.json` build pin**: New committed metadata file (sibling of the existing gitignored `.embody/envoy.json`) records `td_build` — the TouchDesigner version the project was last saved with. `EmbodyExt._writeProjectJson()` writes it on `onProjectPostSave` and once at startup (`onStart` frame 80), idempotent so unchanged builds skip the write. Schema is intentionally minimal (`{"td_build": "2025.32660"}`) to leave room for additional project-level metadata later.
- **Feature: Bridge auto-discovers matching TD install**: `envoy_bridge.py` now globs platform-specific install locations (`C:\Program Files\Derivative\TouchDesigner.*` on Windows, `/Applications/TouchDesigner*.app` on macOS via `Info.plist` `CFBundleShortVersionString`, `/opt/derivative/touchdesigner-*` on Linux) and picks the install matching `project.json`'s `td_build`. Match policy: exact build → same year closest build (warns) → fall back to `envoy.json`'s `td_executable` (warns) → newest installed (warns) → error with download link. Backward compatible — projects without `td_build` use `td_executable` from `envoy.json` exactly as before.
- **Gitignore: `.embody/project.json` is tracked**: `_configureGitignore` switched the managed entry from `.embody/` to `.embody/*` + `!.embody/project.json`. Existing projects auto-migrate on next Embody startup — the bare `.embody/` line is added to `STALE_ENTRIES` and replaced with the negation pair. Project's own root `.gitignore` updated to match.
- **Fix: MCP update-check no longer trips TD thread conflict**: `_checkMCPUpdate()` spawned a worker thread that called `self.Log()` directly on update detection — `Log()` reads `absTime.frame`, reads `self.my.par.Verbose` / `Print`, and appends to a FIFO DAT, all TD object access from a non-main thread, which TD's C++ runtime catches with a "THREAD CONFLICT" dialog naming the Embody COMP. On boot, `_setupEnvironment` runs twice (Start path plus venv-recovery), so two workers race to log "MCP update available" and the loser trips the dialog. Fixed by capturing `owner_path` outside the worker, pre-formatting the message string in the worker, and marshaling the `Log` call to the main thread via `run("o = op(args[0])\nif o: o.Log(args[1], 'WARNING')", owner_path, msg, delayFrames=1)`. The `if o:` guard makes a rename/move between thread spawn and deferred fire a silent no-op.
- **Fix: bridge test debt — 21 stale assertions repaired**: `test_envoy_bridge.py` was carrying assertions left behind by prior bridge refactors. `TestBridgeForwardToHttp` (17 tests) expected a pooled `http.client.HTTPConnection` (`bridge._http_pool`, `bridge._http_pool_lock`, `_get_http_connection`); the bridge had long since been simplified to a fresh `urllib.request.urlopen` per call. setUp / `_make_conn` helper replaced with `_make_response`; tests now mock `urllib.request.urlopen` directly and inspect the `urllib.request.Request` object. `TestBridgeLog.test_log_includes_prefix` was matching the literal `'[envoy-bridge]'` but the bridge format is `'[envoy-bridge:<pid>]'` — assertion now checks the stable `'[envoy-bridge:'` prefix. `TestBridgeMainLoop` (3 initial-connection-timeout tests) assumed v1 semantics where the bridge blocks on `wait_for_envoy` for arbitrary methods; v2 tries `forward_to_http` immediately and only errors when the forward call itself raises — tests now mock `forward_to_http` to raise `OSError`. Bridge tests went from 127/151 → 148/151 passing, zero failures, zero errors (3 explicit stubs skipped).
- **Tests: project.json + TD-install discovery coverage**: New `TestBridgeProjectJsonAndDiscovery` class (15 tests) in `test_envoy_bridge.py` covers `load_project_config()` for missing / valid / malformed / non-dict cases, `_parse_build()` for valid / embedded / invalid inputs, and `select_td_install()` policy (exact match, same-year-closest, fallback-to-envoy-json, fallback-to-newest, no-pin behaviors, nothing-found with and without pin). `find_td_installs()` itself is platform-dependent; tested via the `installs=` injection point. Discovery sanity-checked live on the dev machine — picked up the installed `2025.32460` build, and `_writeProjectJson` correctly wrote the pin to `.embody/project.json` on `onStart`.
- **Dev rules: release-save procedure**: `.claude/rules/release-commits.md` gained a new "Step 0: Save the Project" section documenting that `project.save()` must be called with no arguments. Passing a destination path causes TD's build-increment-on-save to parse the trailing build from *your* path instead of the current `project.path`, desyncing the `.toe` filename suffix from `par.Version` by one. Pre-setting `par.Version` manually has the same effect through a different route. Section also covers recovery if you've already mis-saved (rename `.toe` on disk; close TD without saving and reopen).

## v5.0.386

Batch-confirm prompt for duplicate path detection — one dialog instead of N — so projects with several unresolved duplicate groups no longer spam the user with a modal per group on every save/refresh.

- **Feature: batch-confirm prompt for duplicate paths**: When `checkForDuplicates()` finishes auto-resolving replicants, TD clones, and DATs inside cloned COMPs, any groups it still can't resolve now collect into a list. If 2+ groups remain, a single `Duplicate Paths Detected` dialog appears with three choices: `Dismiss` (skip for now, re-prompt next cycle), `Review individually` (falls back to the existing per-group prompt per group), or `Auto-resolve all (N)` (picks the first listed operator in each group as master; tags the rest with `clone`). Single-group case is unchanged — it goes straight to the original per-group prompt. Addresses user feedback that projects with many copy-pasted COMPs were hitting the per-group modal 5-10 times per save
- **Hardening: `_messageBox` list-of-responses for headless testing**: The test harness's `_smoke_test_responses` storage dict now accepts a list of button indices per title (e.g. `{'Duplicate Path Detected': [1, 1]}`) in addition to the existing single-int form. List values are consumed front-to-back; the key is removed once empty. This unlocks multi-invocation test coverage of the new `Review individually` path where the per-group prompt fires multiple times within a single `checkForDuplicates()` call. Backward compatible — existing single-int seeds still work
- **Tests**: New `TestBatchResolution` class in `test_duplicate_handling.py` with 6 tests covering the single-group shortcut, each batch-prompt button, per-group fallthrough, and the `_autoResolveFirstAsMaster` helper including empty-input safety. Full duplicate_handling suite: 56/56 passing. Verified live in the dev project with 3 fake groups — `Auto-resolve all` correctly kept `alpha_1`, `beta_1`, `gamma_1` as masters and tagged the rest

## v5.0.383

Clone detection fix for self-referencing masters (a common pattern for reusable UI components using `iop.*` expressions), and a cleaner list UI that moves the tree expand/collapse control into a dedicated column.

- **Fix: Self-referencing COMPs are masters, not clones**: `isClone()` and `isInsideClone()` in EmbodyExt were misclassifying reusable-component masters whose `par.clone` evaluates to themselves (a standard pattern — a component COMP sets `par.clone.expr = "iop.Components.op('MyComp')"` so instances dropped elsewhere auto-sync). Before the fix, saving inside such a master would mark DATs as "inside a clone" and route them through the clone-side auto-resolve path, breaking externalization for the component's own authored contents. Both methods now treat `par.clone is self` (identity check on the evaluated op) as a master, not a clone. `isClone()` simplified from "does `oper.name` appear in the stringified clone value" string-match to the direct identity comparison. Added three unit tests in `test_tag_management.py` (`test_isClone_self_reference_is_master`, `test_isInsideClone_self_reference_master_false`, `test_isInsideClone_self_reference_comp_itself_false`) using expression-mode clone assignment to avoid TD's direct-assignment recursion
- **UI: Dedicated expando column in the externalization list**: The tree-expand indicator used to be prefixed onto the network-path cell as a `▸ Name` / `▾ Name` string, which left the path column doing two jobs and misaligned when names varied in length. `list_callbacks.py` now renders a dedicated `+` / `−` character in the leading 16-unit-wide expando column (previously hidden at width 0), leaving the network-path cell to show just the name centered-left with normal padding. Only rows with children get a character; leaf rows stay blank. Small visual change, noticeably cleaner at a glance
- **Chore: `.gitignore` entry for `.release-drafts/`**: Local release-staging directory now ignored

## v5.0.381

Global Perform Mode toggle suspends Embody/Envoy/TDN compute during live performance (Issue #13), auto-resolve for duplicate DATs inside active clones without prompting (Issue #15), ancestor-rename disk handling fixed so `Move` no longer fails with "source folder not found" (Issue #16), and new render-coordinate-system rules documenting TD's bottom-left origin convention (Issue #14).

- **Feature: Perform Mode** (Issue #13, reported by Chris Mills): New `Performmode` toggle on the Embody COMP (and perform button in the toolbar) suspends all Embody/Envoy/TDN compute for the duration of a live performance. On enter, `_enterPerformMode` snapshots pre-state (Envoy running, keyboard listener active, exit tagger active) and stops Envoy directly, disables the `keyboardin1` DAT and `chopexec_exit_tagger`, closes the manager window, greys out Envoy parameters, and sets `Envoystatus = 'Perform Mode'`. Guards added to `Update`, `Refresh`, `Save`, `SaveTDN`, `SaveCurrentComp`, `TagGetter`, `ExternalizeProject`, `getDirtyCount`, `onProjectPreSave`, `onProjectPostSave`, and Envoy's `_onServerSuccess`/`_onServerError` auto-restart. `_exitPerformMode` restores snapshot state and restarts Envoy if it was running. `execute.py:onCreate` clears `Performmode = False` on project open so the toggle never persists across sessions. Envoy parameter changes to `Envoyenable`/`Envoyport`/`Aiclient` are protected (never touched during Perform Mode, so config.json stays intact)
- **Fix: Auto-resolve duplicate DATs inside active clones** (Issue #15, reported by Chris Mills): When a COMP with an externalized DAT inside is cloned, the master's DAT and the clone's DAT share the same relative path — producing a duplicate prompt on every save. `_resolveDATsInClonedCOMPs()` now auto-resolves these groups without prompting: DATs inside an active clone COMP are treated as references (clone-side), DATs in the master are kept as the master. Wired into the duplicate resolution flow in `cleanupAllDuplicateRows()` alongside the existing `_resolveClonesByCloningAPI()` handler
- **Fix: Ancestor rename no longer fails with "source folder not found"** (Issue #16, reported by Chris Mills): `_handleAncestorRename()` was building disk paths from the raw operator-path prefix (e.g. `/old` → `old`) and passing that straight to `project.folder / old`. That works by coincidence when `Externalizationsfolder` is empty (the default — files write directly under the project root) because the op-path segment and the on-disk segment match. The moment `Externalizationsfolder` is pointed at a subfolder (say `ext/`), files actually live at `project.folder / ext / old / ...` but the rename code was still looking at `project.folder / old / ...` — so `old_dir.exists()` returned False and the user saw "Source folder not found." The method now composes `Externalizationsfolder` into the disk segment before every filesystem operation (Phase A rel_file matching, Phase C directory rename, Phase D table updates, TDN-strategy handling, user cancellation path all fixed). Returns `bool` so `checkOpsForContinuity()` can fall back to per-operator handling when the ancestor-level rename fails for any reason
- **Hardening: Clone detection null-safety**: `isInsideClone()` and `isClone()` now use `getattr(par, 'clone'/'enablecloning', None)` with exception wrapping so DATs and operators that lack those parameters no longer raise during duplicate resolution. Also excludes DATs inside clone COMPs from the path-groups collected by `_buildPathGroups()` so replicant filtering and duplicate detection agree
- **UI: Perform button in toolbar**: New `perform` textCOMP (Material Design icon) between Status and Disable buttons, wired to `ToolbarExt._action_toggle_perform()`. Tinted amber when active (face color driven by `Performmode` parameter, matching the Disable button's active-state pattern). Keyboard shortcut suppression, exit-tagger gating, and parexec routing to `_enterPerformMode`/`_exitPerformMode` all fire from the single Performmode toggle
- **UI: Full Envoy status string in toolbar**: `envoy_status` widget now reads the full `Envoystatus` parameter (`"Running on port 9870"`, `"Off"`, `"Error: ..."`, `"Perform Mode"`) instead of just the port number. Width expanded 55 → 160 units. Text color unchanged (uses default `Textcolor`, not green). Window header `min_width` bumped 410 → 440 to accommodate the new button; title now concatenates `Headerlabel + '  ·  ' + Envoystatus` so project name and MCP status are visible from any docked pane
- **Rule: Render coordinate system** (Issue #14, reported by Chris Mills): Added "Render Coordinate System" section to `.claude/rules/td-python.md` and expanded "TOP Pixel Access" in `skills/td-api-reference/SKILL.md` documenting TD's bottom-left origin convention. `TOP.sample(x, y)` y=0 is the bottom edge, GLSL `gl_FragCoord.y=0` is the bottom, UV and crop/transform params are bottom-left, but `TOP.numpyArray()` returns rows top-to-bottom and PIL/OpenCV/panel coords are all top-left. Table + `np.flipud()` guidance added. Templates in `dev/embody/Embody/templates/` synced so user projects get the new guidance on Embody initialization
- **Log: Fix pluralization in auto-resolve log line**: `_resolveDATsInClonedCOMPs()` log message was using `len(clones) != 1` as the plural guard, producing "0 DATs" with an errant s in the no-clones path. Changed to `len(clones) > 1`
- **Test: 48 test suites** (+1): New `test_ancestor_rename.py` (680 lines) covers `_detectAncestorRename` threshold and prefix extraction, `_handleAncestorRename` Phase A/C/D on externalized COMPs, disk segment composition with `ExternalizationsFolder`, TDN-strategy handling, user cancellation, fallback to per-operator on failure, and full end-to-end rename flow with directory movement verification. `test_duplicate_handling.py` expanded (+70 lines) to cover `_buildPathGroups` replicant filtering, `_resolveClonesByCloningAPI` non-COMP handling, `_resolveDATsInClonedCOMPs` auto-tagging, and dialog-driven master/clone selection. `test_tag_management.py` expanded (+57 lines) to cover `isInsideClone` null-safety on DATs without `par.clone` and `isClone` active-vs-master discrimination

## v5.0.376

Palette scan no longer triggers invasive palette popups (TDVR framerate warning, AutoUI widget-package dialog) on fresh-build startup, rebaked palette catalog for TD 2025.32460, and Issue #12 fix for false "locked content" warnings inside clones and replicants.

- **Fix: Palette scan skips invasive palettes (TDVR, AutoUI)**: `CatalogManagerExt._startPaletteScan()` now filters a small blocklist (`tdvr`, `autoui`) before `loadTox`. When `palette_catalog` bootstrap doesn't cover the current TD build, the runtime scan used to load every palette .tox into a hidden workspace — including TDVR (which unconditionally calls `project.cookRate = 90` and pops a messageBox) and AutoUI (which pops a "Widget Package Required" dialog). Both were blocking main-thread modals that scared users into thinking Embody had taken over their project. Loss of palette-clone detection for these two components is acceptable — they're rare in TDN-diffed networks and were silently broken anyway. Single log line names what was skipped
- **Rebake: `palette_catalog.tsv` now covers build 099.2025.32460**: Ran `ExportPaletteCatalog()` on current stable TD; the shipped bootstrap table now includes 261 palette components for 32460 alongside the existing 264 for 32280 (525 data rows + header, ~22 KB). Users on either build hit the bootstrap and skip the palette scan entirely on first load — no workspace creation, no `loadTox` calls, no popups
- **Fix: False "locked content" warnings inside clones and replicants** (Issue #12, reported by Chris Mills): `TDNExt._checkLockedUnexportedContent()` now skips operators whose ancestor chain contains a clone master (`clone` + `enablecloning` both set) or a replicant template. Lock state inside clones is inherited from the master, not owned by the instance; lock state inside replicants is regenerated per-template by the replicator COMP. Warning the user about those paths is noise, not signal — and the paths (e.g. `icon (TOP)`) were especially confusing because they don't exist at the root level the warning referenced. Added helper `_isInsideCloneOrReplicant()`. Also switched summary from `child.name` to `child.path` so any remaining warnings point to an unambiguous location

## v5.0.372

TDN master switch becomes a three-mode menu (Off / Export-on-Save / Roundtrip) replacing the short-lived `Tdnenable` toggle, new `read_tdn` MCP tool for 20-90× token-cost reduction on multi-operator reads, combined DAT+storage Content Safety dialog, palette-detection fix for native `buttonCOMP` operators, and a docs + landing page rewrite making the TDN value proposition explicit.

- **Feature: `Tdnmode` three-way menu**: New `Tdnmode` menu on Embody's TDN page with three values. *Off* disables the entire TDN subsystem (no export, no reconstruction, no catalog scan — fastest startup for projects that don't use TDN). *Export-on-Save* (new default) writes `.tdn` files on save for diffs and AI context, but does not rebuild COMPs from `.tdn` on open — the `.toe` remains authoritative. *Roundtrip (Experimental)* is the full previous behavior: export on save plus reconstruct TDN-strategy COMPs from disk on open. Gates every TDN entry point (`SaveTDN`, `Update()` TDN loop, `ReconstructTDNComps`, pre-save strip, `CatalogManager.EnsureCatalogs`). Internal `menuNames` stay as `off`/`export`/`full` so persisted values and code references don't churn
- **Feature: Migration nudge for upgrading users**: On first open after upgrade, projects saved with the legacy `Tdnenable` toggle see a one-shot dialog explaining the new mode and defaulting them to Export-on-Save (or offering a one-click restore of their previous Full behavior as Roundtrip). Stored flags (`_tdn_mode_migration_shown` + `_tdn_migration_scheduled`) prevent re-prompting and double-firing if `_restoreSettings` is called twice within the 60-frame defer window
- **Feature: `read_tdn` MCP tool**: New MCP tool returns a COMP's live network as a TDN dict without writing to disk. Typically **20-90× fewer tokens** than walking the same subtree via `get_op` + `query_network` thanks to default omission, `type_defaults`, and `par_templates` compaction. Intended as the preferred read path for LLM workflows exploring networks of more than ~3 operators. Works in all three `Tdnmode` values (reads live state, not disk). Scope cost via `comp_path`; cap with `max_depth`. Docstring enumerates when NOT to use (runtime values → `get_parameter`, cook errors → `get_op_errors`, DAT/TOP data → `get_dat_content`/`capture_top`, etc.). Conservative 5× floor verified in CI
- **Feature: Combined Content Safety check (`Tdndatsafety` → "Content Safety")**: Pre-save safety gate now inspects both DAT content AND `comp.storage` for at-risk user data inside TDN-strategy COMPs, surfaced in one combined dialog. `_findAtRiskStorage` mirrors `_findAtRiskDATs`. Parameter renamed from "DAT Safety" to "Content Safety" to reflect the expanded scope. `_STORAGE_SKIP_KEYS` covers Embody's internal runtime keys (`_tdn_stripped_paths`, `_tdn_palette_handling`, migration flags, etc.) so only user-owned keys surface
- **Feature: Removed "Never Ask" dialog button**: The Content Safety dialog no longer offers a single-click "Never Ask" footgun. *Ignore* remains available as a menu value on the `Tdndatsafety` parameter for power users who explicitly opt out, but the accidental-dismiss path that silently disarmed all future checks is gone. Dialog is now 3 buttons: *Externalize DATs* / *Skip* / *Always Externalize*. Skipped content is logged at SUCCESS level with the exact op paths and keys that were dropped
- **Fix: Palette detection false-positive on native `buttonCOMP`**: `TDNExt._isPaletteClone()` was misclassifying stock TD operators like `buttonCOMP` as palette clones because every freshly-created COMP clones from `/sys/TDTox/defaultCOMPs/<type>` by default, and the `/sys/` prefix matched the Strategy 2 heuristic. Detection now explicitly excludes `/sys/TDTox/defaultCOMPs/` paths and `'defaultCOMPs'` in the clone expression. Native COMP types export their internals normally; real palette clones (TDBasicWidgets, TDResources, actual Palette sources) still match
- **Fix: `onProjectPostSave` regression in Off/Export modes**: Post-save used to early-return when `_tdn_stripped_paths` was empty — which never happened in the `Tdnenable=True` world but happens on every Off/Export save. The early return skipped `_init_complete` re-store, silently disabling every parexec callback for the rest of the session. Strip-restoration is now guarded by `if stripped:`; pane restore, `_init_complete` re-store, and the delayed `Refresh.pulse()` always run
- **Fix: Envoy restart conditional on strip having happened**: Post-save Envoy restart was made unconditional during the above fix, which meant every Off/Export save was needlessly tearing down and restarting the MCP server thread. Now gated on `stripped and Envoyenable.eval()` so restart only fires in Roundtrip mode where the extension actually reinitialized
- **Fix: Cancel path on Off transition no longer double-logs**: When a user flips Tdnmode to Off with tracked TDN COMPs and picks Cancel, the revert to `export` now happens with parexec suppressed so the transition handler doesn't re-fire and emit a misleading "mode: Export-on-Save" INFO immediately after the "cancelled by user" message. Also silences the "TDN disabled" log when flipping to Off with zero tracked COMPs
- **Perf: Catalog load gated on `Tdnmode != 'off'`**: `CatalogManager.EnsureCatalogs()` skipped entirely when `Tdnmode = Off`. The catalog is consumed exclusively by TDN export compaction and palette-clone detection — both dormant in Off. Saves the op-type scan + divergent-defaults probe at startup for users who don't need TDN
- **Docs: TDN Strategy section rewrite**: `docs/embody/externalization.md` replaces the binary `Tdnenable` narrative with a three-mode table + a new **Why TDN** subsection covering file size/density, git three-way merge, PR review, cross-version portability, CI/CD schema validation, and the 20-90× MCP token cost reduction. Grounded in real TDNExt code paths (default omission, type_defaults, par_templates) and actual `.tdn` file sizes. `configuration.md`, `getting-started.md`, `troubleshooting.md` updated to match. In-app help text on `Tdnmode` and `Tdndatsafety` rewritten. `read_tdn` added to every MCP tool catalog page. `/sys/TDTox/defaultCOMPs/` exclusion documented in the TDN specification. Migration nudge described in the config reference
- **Docs: Landing page (embody.tools) positioning rewrite**: `web/embody/index.html` TDN Strategy feature card reframes TDN as a mirror of the `.toe` rather than a replacement. The "bidirectional sync" pillar becomes two pillars — *export on save* (the default) and *roundtrip (experimental)*. `web/tdn/index.html` Embody pillar softened to match. `web/envoy/index.html` adds a new `read_tdn` feature card highlighting the 20-90× token reduction; tool count updated from 46 to 47. Sample TDN JSON generator strings bumped to `Embody/5.0.372`
- **Test: 47 test suites** (+3, 1184 test cases): `test_tdn_mode` (15 tests covering all three modes, gating, reconstruction/SaveTDN guards, regression guards for `_init_complete` and Envoy restart), `test_tdn_safety_guards` (7 tests covering `_findAtRiskStorage`, combined dialog, Never-Ask removal, skip logging), `test_mcp_tdn_tools` (5 tests covering `read_tdn` round-trip, mode agnosticism, DAT content toggle, and a token-budget regression with a conservative 5× CI floor)

## v5.0.362

Palette handling control during TDN export, CatalogManager robustness on fresh project drops, palette catalog portability and log-level fixes.

- **Feature: TDN palette handling** (`Tdnpalettehandling`): New menu parameter on Embody's TDN page controls how palette COMPs are handled during TDN export. *Ask* (default) prompts on first encounter per COMP with a four-button dialog — *Black Box* (this COMP), *Full Export* (this COMP), *Black Box for All* (flips the project-wide par), *Full Export for All* (flips the project-wide par). *Black Box* always references the palette and skips internal children (correct for stock palette COMPs; lets upstream Derivative palette updates flow through on round-trip). *Full Export* always exports all internals (for heavily customized palette COMPs). Per-COMP decisions are persisted via `comp.store('_tdn_palette_handling', …)`, so you aren't re-prompted for the same COMP. Implementation: `TDNExt._resolvePaletteHandling()`, `TDNExt._promptPaletteHandling()` consult per-COMP storage → par value → prompt
- **Fix: CatalogManager on fresh project drops**: `EnsureCatalogs()` is now called from `execute.py:onCreate` at frame 45 in addition to `onStart`. Previously new users dropping the `.tox` had empty divergent defaults and broken palette detection in their first session — the catalog only loaded on project reopen
- **Fix: Catalog scan stall ("N/N" forever)**: `CatalogManagerExt._log()` was missing a `level` parameter. A v5.0.358 logging call passing `'DEBUG'` as second arg caused a TypeError that silently killed `_finalizeScan`, leaving catalog scans stuck with no catalog written. `_log(msg, level='INFO')` now accepts optional level
- **Fix: Scan finalize defensively in-band**: `_processChunk` and `_processPaletteChunk` now finalize the scan when the queue empties instead of relying on a scheduled `run(delayFrames=1)` callback for the final tick. Defends against lost callbacks during heavy concurrent startup (venv creation, dialog auto-response, Envoy server start)
- **Fix: `palette_catalog` tableDAT portability**: Both the DAT's `file` par and the row in `externalizations.tsv` had an absolute path (broken on other machines). Now uses relative `embody/Embody/palette_catalog.tsv` + `syncfile=True` + `file.readOnly=True`, matching the `divergent_defaults` pattern
- **Rename: `CheckAndScan()` → `EnsureCatalogs()`**: Clearer intent-verb name. Method is now idempotent — safe to call repeatedly, returns early when already populated
- **Fix: Catalog scan errors demoted to DEBUG**: Abstract base types (`td.CHOP`, `td.DAT`, etc.) that can't be instantiated bare were logging at INFO on every startup. Non-actionable for users
- **Fix: Gitignore migration noise**: `.envoy-tools-cache.json` removed from stale-entries migration list — was being flagged on every startup despite being intentionally kept
- **Rule: Naming — Methods, Functions, Operators**: New section in `td-python.md` + template covering intent-verb naming, avoiding `CheckAndX`/`DoStuff`/implementation-leakage patterns, boolean `is/has/can` phrasing, public-vs-private conventions
- **Docs**: Updated configuration.md, externalization.md, TDN specification.md, in-app help text, and `externalize-operator` skill to cover palette handling and the shipped palette catalog mechanism
- **Test: 44 test suites** (+1, 10 new G01-G10 tests in `test_tdn_palette_catalog` covering the palette handling resolver, prompt flow, per-COMP storage override, and end-to-end export behavior)

## v5.0.356

Palette catalog detection, animationCOMP keyframe preservation, external wire preservation across TDN strip/rebuild, Envoyenable startup fix.

- **Feature: Palette component catalog**: `CatalogManagerExt` now walks TD's shipped palette directory after the op-type scan, loads each `.tox` into a temp workspace, and records `{name: {type, min_children}}`. `TDNExt._isPaletteClone()` uses this catalog as the primary detection method (name + OPType + child-count floor), falling back to the clone-expression heuristic (now covers `TDBasicWidgets` in addition to `TDResources`/`TDTox`/`/sys/`). Catches palette components whose clone reference was never set while avoiding false positives from user COMPs that happen to share a palette name
- **Feature: animationCOMP keyframe preservation**: DATs inside an `animationCOMP` (`keys`, `channels`, `graph`, `attributes`) always export their content regardless of the `include_dat_content` option. Previously these read-only-looking tableDATs lost all keyframe data on TDN round-trip
- **Fix: External connection preservation across TDN strip/rebuild**: Wires from external siblings into a TDN-strategy COMP's own input/output connectors (backed by internal `in*`/`out*` operators) were severed when the COMP's children were destroyed during save's strip/restore cycle, cold open, or manual reimport. `StripCompChildren` now captures external wires via `comp.store()` before destruction; `ImportNetwork(clear_first=True)` restores them after rebuild (and also captures live wires directly when called without a prior strip)
- **Fix: Envoyenable disabled on every startup**: `init()` stored `_init_complete` immediately after setting `Envoyenable = False`, but TD defers `onValueChange` callbacks to the next cook — so parexec processed init's own `Envoyenable=False` change and called `Stop()`. `_init_complete` is now stored by `_restoreSettings` after restoration completes (or immediately on its early-return paths), keeping parexec suppressed through the deferred callbacks
- **Fix: Catalog path mismatch**: `CatalogManagerExt._findProjectRoot()` now delegates to `EmbodyExt._findProjectRoot()` which walks up from `project.folder` looking for `.git`. Previously `project.folder` (often `dev/`) differed from the git root and produced duplicate catalogs under different paths
- **Fix: Abstract type scan rejection**: `td.CHOP`, `td.COMP`, `td.DAT`, etc. are abstract base types with suffixes matching `_FAMILIES` but aren't creatable. Added `_ABSTRACT_TYPES` filter to skip them during catalog scan
- **Test: 43 test suites** (+2): `test_tdn_palette_catalog` (catalog lookup, child-count floor, TDBasicWidgets heuristic, animationCOMP round-trip), `test_tdn_external_connections` (strip+import restore, live-wire capture, deleted-sibling tolerance)
- **Gitignore**: Added `.envoy-tools-cache.json` (bridge tool cache — runtime artifact)

## v5.0.354

Consolidate all Embody/Envoy runtime files into a single `.embody/` folder.

- **Refactor: `.embody/` folder consolidation**: All auto-generated runtime files now live in one gitignored folder instead of scattered dotfiles at the project root. `.envoy.json` → `.embody/envoy.json`, `.embody.json` → `.embody/config.json`, `.envoy-tools-cache.json` → `.embody/envoy-tools-cache.json`, `.claude/envoy-bridge.py` → `.embody/envoy-bridge.py`. Makes Envoy fully client-agnostic -- no Envoy artifacts in `.claude/`
- **Migration: automatic upgrade from old paths**: On first Envoy start after upgrade, existing config files are read from old locations, seeded into `.embody/`, and old files removed. `.gitignore` stale entries (`.envoy.json`, `.embody.json`, `.claude/envoy-bridge.py`, etc.) are automatically replaced with a single `.embody/` entry
- **Fix: bridge path resolution**: `resolve_toe_path()`, `_heartbeat_path()`, `_init_log_file()`, `_find_stale_bridges()`, and `_validate_and_resolve()` now correctly resolve paths relative to the git root (one level up from `.embody/`) instead of the config file's parent directory
- **Docs**: Updated architecture, setup, claude-code, tools-reference, configuration, getting-started, multi-instance skill, and td-connectivity rule to reflect new paths

## v5.0.352

Fix Envoy failing to start after Embody upgrade (delete old COMP, drop new .tox).

- **Fix: Envoy restart counter not resetting on upgrade**: When the old server's port wasn't released in time, auto-restart exhaustion left `_restart_count` stuck above MAX. The next manual Envoyenable toggle immediately hit the limit and forced itself back to False, making the toggle appear to "do nothing." `Stop()` now always resets `_restart_count`, even when `envoy_running` is already False
- **Fix: Upgrade-path port race**: `Verify()` deferred `Start()` by only 10 frames (~0.17s) after the old COMP was deleted -- too short for uvicorn to fully release its listener socket. Increased to 60 frames (~1s)
- **Fix: Port reclaim timeout too short**: `_findAvailablePort()` waited only 0.5s for a force-closed port to become available. Increased to 1.5s to accommodate uvicorn's shutdown sequence

## v5.0.351

Creation-defaults catalog, stdin-based bridge lifecycle, Envoy resilience hardening.

- **Feature: Creation-defaults catalog (`CatalogManagerExt`)**: TD's `p.default` lies for dozens of parameters (e.g., cameraCOMP `tz`: `p.default=0` but creation value is `5`). Embody now scans all creatable op types at startup (1-2 ops/frame, non-blocking), writes a per-build catalog to `.embody/`, and uses actual creation values for TDN export/import. Fixes silent data loss where user-set values matching the wrong default were omitted from export
- **Feature: Cross-build default patching**: When opening a project exported on a different TD build, the CatalogManager compares catalogs and patches any parameters whose creation defaults shifted between builds. Shows a summary dialog of all corrected values
- **Feature: Divergent defaults fallback**: Embedded `divergent_defaults.tsv` table provides bootstrap data for known TD builds. On-the-fly probing handles unknown builds by creating temp operators and comparing `p.val` vs `p.default`
- **Fix: Bridge orphan detection**: Replaced ppid-based orphan watchdog (broken under VS Code extension host, which outlives sessions) with stdin pipe POLLHUP detection via `select.poll()` (macOS/Linux) and `PeekNamedPipe` (Windows). Bridges now exit reliably when their Claude Code session closes
- **Fix: Bridge stale process cleanup**: Heartbeat files (`envoy-bridge-{pid}.heartbeat`) replace parent-PID heuristics for detecting stale bridges. Phase 1 kills bridges with stale heartbeats (>60s old); Phase 2 falls back to legacy orphan check for pre-heartbeat bridges
- **Fix: Bridge heartbeat simplification**: Replaced dynamic fast/slow heartbeat cadence (5s/30s) with fixed 10s interval. Removed HTTP connection pool (reverted to simple `urllib.request.urlopen` per call) — the pool caused persistent "not responding" errors from half-closed `http.client` connections
- **Fix: Envoy queue persistence across save cycles**: Request/response queues now survive extension reinit during Ctrl+S by persisting in `sys._envoy_queues`. Prevents lost MCP requests during the strip/restore window
- **Fix: Envoy auto venv recreation**: Corrupted venv (broken Python path after TD upgrade) is now auto-recreated once per session instead of just logging a warning
- **Fix: ClientDisconnect suppression**: Added starlette `ClientDisconnect` to suppressed exceptions alongside `BrokenResourceError`/`ClosedResourceError`. Prevents traceback floods from destabilizing uvicorn's event loop during extension reinit or tab close
- **Fix: Scan workspace cleanup**: On-the-fly default probe workspace (`_defaults_workspace`) is now destroyed after each export. Previously leaked empty baseCOMPs that accumulated in the Embody COMP across saves
- **Improved: Em-dash to double-dash**: Systematic `—` to `--` replacement across all Python source files for cross-platform DAT encoding safety
- **Improved: FastMCP log filtering**: Suppresses empty "Received exception from stream:" messages from recycled bridge connections
- **Test: 41 test suites** with 4 new divergent-defaults tests (cameraCOMP tz, lightCOMP tz, renderTOP resolution round-trip, false-positive prevention)

## v5.0.336

Batch MCP operations, Envoy auto-restart on crash and save, 46 MCP tools.

- **Feature: `batch_operations` MCP tool**: Combine multiple tool calls into a single request — positions, connections, parameters, flags, etc. Stops on first error, returns per-operation results. Cuts token overhead and latency for repetitive operations
- **Fix: Envoy dies on Ctrl+S**: The save cycle's TDN strip/restore killed the server thread via extension reinit, leaving status stuck on "Running" with a dead port. `onProjectPostSave` now explicitly restarts Envoy after restoration completes
- **Fix: Envoy auto-restart on crash**: Server thread failures (SuccessHook/ExceptHook) now trigger automatic restart with exponential backoff (1s, 2s, 3s) up to 3 attempts. Counter resets after 2 minutes of stable uptime. Manual Stop() resets the counter
- **Rule: Batch repetitive MCP operations** (CLAUDE.md #12): Never make 3+ individual calls to the same tool — use `batch_operations` or `execute_python` instead
- **Test: 41 test suites** including new `test_mcp_batch` (9 tests covering success, error handling, nested prevention, practical create+query patterns)

## v5.0.330

Envoy bridge v2: proactive reconciliation, multi-session safety, and zero forced restarts. The bridge now survives TD crashes, instance switches, and multi-session concurrency without requiring Claude Code session restarts.

- **Feature: Background reconciler thread**: Polls `.envoy.json` every 1 second (unconditionally, regardless of connection state) and pings the backend every 5–30 seconds (dynamic backoff). Detects instance switches within seconds — opening a new TD instance mid-session automatically routes MCP calls to the new instance
- **Feature: Disk-based tool cache**: Persists the full tool list to `.envoy-tools-cache.json` so new sessions always start with all 45+ tools, even if TD hasn't finished loading. Works around Claude Code's `list_changed` notification bug (#13646)
- **Feature: HTTP connection pooling**: Replaced per-request `urllib.request.urlopen()` with a persistent `http.client.HTTPConnection` per URL. Eliminates socket churn that was causing `ClientDisconnect` tracebacks in starlette and crashing Envoy's HTTP server under load
- **Feature: Dynamic heartbeat backoff**: Pings every 5s while unstable or recently changed, slows to 30s once connected stably for 30+ seconds. Reduces textport noise by 6x in steady state
- **Feature: Proactive TD process discovery**: `find_all_td_pids()` scans for new TouchDesigner processes every heartbeat, forces config re-read when new TDs appear. Filters out bridge processes that use TD's bundled Python (false positive fix)
- **Feature: `notifications/tools/list_changed` emission**: Bridge advertises `listChanged: true` and sends the MCP notification on every backend state transition and explicit instance switch
- **Feature: Local `ping` handler**: Answers MCP `ping` requests locally with zero latency, regardless of backend state
- **Feature: Multi-session safety**: `kill_stale_bridges()` now checks parent PID before killing peers — only orphans (parent dead/reparented to launchd) are terminated. Multiple Claude Code sessions can safely coexist against the same project
- **Fix: Port conflict detection in multi-instance startup**: `_findAvailablePort()` now checks the `.envoy.json` registry in addition to socket probes, preventing two TD instances from racing on the same port during near-simultaneous startup
- **Fix: Restart loop on port fallback**: Removed `Envoyport` parameter update during `Start()` that triggered `parexec.py` Stop+Start cycle when the port shifted (e.g., 9870→9871)
- **Fix: Ghost TD detection**: `find_all_td_pids()` now excludes bridge processes whose cmdline contains `envoy-bridge`, preventing false "TD is alive" reports when only bridge processes remain
- **Fix: Orphan watchdog hardening**: Added `is_process_alive(parent_pid)` belt-and-suspenders check alongside ppid comparison, catches cases where ppid doesn't update immediately on reparenting
- **Improved: 3-second initial probe** (was 60s): First `tools/list` response returns in ≤3 seconds with the best available tools (live, cached, or bridge-only). Reconciler handles recovery in the background
- **Improved: Single-attempt forwarding** (was 4 retries): Failed MCP forwards return immediately instead of blocking 7.5 seconds on retries. The reconciler drives reconnection
- **Improved: PID-tagged log lines**: `[envoy-bridge:PID]` format makes multi-session logs distinguishable
- **Improved: PID-tagged temp files**: `atomic_write_json()` uses per-PID temp files to prevent collisions between concurrent bridge processes
- **Improved: Server-side log filter**: Suppresses FastMCP's per-request `Processing request of type PingRequest` messages from flooding TD's textport
- **Test: 136 bridge unit tests** across 19 suites, covering BridgeState locking, tool hash detection, reconciler state transitions, listChanged capability, cache hits, stdout serialization, single-attempt forwarding, and connection lifecycle

## v5.0.320

TDN v1.3: parameter sequence round-trip + companion DAT handling. Operators with resizable parameter blocks (mathmixPOP, glslPOP, constantCHOP, etc.) and companion DATs (GLSL `_pixel`/`_compute`/`_info`, Timer/Script CHOP `_callbacks`, Ramp TOP `_keys`, etc.) now round-trip cleanly through TDN export/import.

- **Feature: TDN parameter sequence support** (TDN v1.3): Operators with built-in parameter sequences (mathmixPOP Combine blocks, glslPOP/glslTOP uniform sequences, attributePOP attribute blocks, constantCHOP channel blocks, etc.) now export their sequence data in a new `sequences` key and restore it on import. Previously, adding parameter blocks (e.g., a new Combine block on mathmixPOP) would silently lose the added blocks after TDN round-trip
- **Feature: Custom parameter sequence support**: Custom sequences defined via `page.appendSequence()` are now round-tripped correctly. Template parameters are exported with their base name and a `sequence` field; on import, `blockSize` is set from the template par count before `numBlocks` populates the block instances. Includes a fallback resolver for custom-sequence block parameters where TD's `block.par.{base}` lookup returns `None`
- **Feature: Read-only DAT detection**: Auto-generated companion DATs (e.g. `glsl1_info`, `popto1`) that reject `dat.text = ...` writes are now probed at export time and tagged with `dat_read_only: true`. Their content is excluded from the export, and importers no longer log "not editable" warnings when restoring them. Older `.tdn` files without the flag are also handled silently
- **Fix: Parameter cache silently dropping sequence parameters**: `_buildParCache()` cached exportable parameters per OPType from the first instance encountered. Sequence parameters with dynamic names (e.g., `comb2oper` on a 3-block mathmixPOP) were silently skipped on other instances whose block count exceeded the cached set. Sequence parameters are now excluded from the flat parameter cache and handled by the dedicated sequence export path
- **Improved: Import Phase 2.5**: New `_expandSequences()` phase runs between custom parameter creation (Phase 2) and parameter value setting (Phase 3), ensuring dynamically-created sequence parameter slots exist before values are applied
- **Improved: Network layout rule — Docked Callback DATs**: New section in `.claude/rules/network-layout.md` (and the matching template) defines a deterministic placement formula for the companion DATs that TD auto-spawns and docks to operators (chopExecuteDAT, glsl info DATs, keyboardinDAT, etc.). Includes a center-out alternation pattern and a procedure for repositioning every dock after `create_op`
- **Test: Sequence round-trip tests**: 12 new tests in `test_tdn_sequences.py` covering export format, round-trip fidelity, expression values, nested COMPs, type_defaults exclusion, and backward compatibility
- **Test: Companion DAT round-trip tests**: 14 new tests (Section W) in `test_tdn_reconstruction.py` covering GLSL TOP/multi/POP/copy/advanced companions, Timer/Script CHOP/SOP/DAT callbacks, Ramp TOP keys, read-only info DAT handling, and a comprehensive no-duplicates check across all companion-creating ops

## v5.0.310

Fix first-time Envoy setup permanently stuck on "Enabled + Disabled" (issues #8, #9).

- **Fix: Envoy permanently stuck "Disabled" after first-time install** (GitHub issue #9): `_init_complete` was stored as an instance attribute on EmbodyExt, destroyed when file sync recompiled DATs during first-time setup. Parexec silently dropped all parameter changes — including `Envoyenable = True` — so `Start()` was never scheduled. Moved `_init_complete` to COMP storage (`.store()`/`.fetch()`) which survives extension reinit. Added pre-save unstore and post-save re-store to prevent baking into the `.tox`
- **Fix: `Start()` status guard self-poisoning** (GitHub issue #9): `EnvoyExt.__init__` set `Envoystatus = 'Starting...'` before deferring `Start()` by 30 frames. `Start()` then saw `'Starting...'` in its status guard and assumed another start was in progress — permanent deadlock. Removed the premature status from `__init__`; narrowed the guard to only block on `'Running'` (actual server activity), not `'Starting...'` (UI hint)
- **Fix: `.gitignore` and `.gitattributes` not generated on first-time git init** (GitHub issue #8): Git config files are now created inside `_checkOrInitGitRepo()` immediately after `git init` succeeds, instead of relying on `Start()` which may not run in the same session
- **Fix: Type error in `Start()` git config** (pre-existing): `_configureGitignore`/`_configureGitattributes` expect a `Path` object but `Start()` passed a string from COMP storage. Wrapped with `Path()` conversion
- **Improved: `Start()` status guard visibility**: Upgraded the `Envoystatus` backup guard from DEBUG to WARNING so state inconsistencies are visible in logs

## v5.0.305

Replicant duplicate detection fix (issue #4 update), TDN export improvements, ExternalizeProject dialog enhancement.

- **Fix: Replicant duplicate detection** (GitHub issue #4 follow-up): `_buildPathGroups()` now filters out replicants alongside clones, preventing replicator outputs from entering the duplicate detection flow. Previously, 100 replicants sharing the same `externaltox` path would trigger a massive popup with 100+ buttons. Added `_resolveReplicants()` safety net that auto-tags replicants as clones without prompting if they reach `checkForDuplicates()` through another code path
- **Improved: ExternalizeProject dialog**: Expanded the "Externalize Full Project" dialog with clearer descriptions and new combined options (`TOX + Project TDN`, `TDN + Project TDN`) that externalize operators and also export a project-wide `.tdn` snapshot in one step
- **Improved: TDN export `source_file` field**: All TDN exports now include the originating `.toe` filename for traceability
- **Improved: Stable project TDN filenames**: New `_stripBuildSuffix()` strips the auto-incrementing build number (e.g. `.302`) from project names, so root TDN exports produce a stable filename across saves (e.g. `Embody-5.tdn` instead of `Embody-5.305.tdn`)
- **Test: Replicant handling tests**: 4 new tests in `test_duplicate_handling.py` covering `_resolveReplicants`, `isReplicant`, and replicator integration with `_buildPathGroups`
- **Test: TDN file I/O tests**: 7 new tests in `test_tdn_file_io.py` for `_stripBuildSuffix` edge cases and `source_file` export verification

## v5.0.302

Fix duplicate path clone detection (issue #4), config file location (issue #5), Envoy startup flow on fresh .tox install.

- **Fix: Clone assignment for duplicate paths** (GitHub issue #4): Rewrote duplicate detection to use group-based path mapping (`_buildPathGroups`) and TD's `.clones`/`par.clone` API for automatic master identification. COMPs that are clones of each other are resolved silently; non-clone duplicates show a single per-group dialog with Dismiss option. Eliminated infinite cancel loop and wrong-operator tagging
- **Fix: Config files written to home directory** (GitHub issue #5): Bounded `_findProjectRoot()` and `_checkOrInitGitRepo()` walk-up to stop at `Path.home()`, preventing accidental discovery of unrelated git repos (e.g. dotfiles in `~`). Added `_git_prompt_active` guard against concurrent git dialogs
- **Fix: Envoy auto-start on fresh .tox drop**: `EnvoyExt.__init__` was scheduling `Start()` based on the baked `Envoyenable=True` before `init()` could reset it. Added `_init_complete` guard so auto-start only fires during extension reinit in a running session, never on fresh install. Removed `_setupEnvironment()` from `EmbodyExt.__init__` (now runs inside `Start()`)
- **Fix: Envoy opt-in prompt not appearing**: `_restoreSettings()` finding a leftover `.embody.json` caused `Verify()` to skip the "Enable Envoy?" dialog. Fresh installs (empty externalizations table) now always prompt, regardless of prior settings files
- **Fix: Sequential dialog flow**: Moved git repo check into `_enableEnvoy()` so it runs immediately after the user clicks "Enable Envoy" — before deps install. `Start()` now uses silent `_findGitRoot()` and never shows dialogs
- **Fix: Runtime-only storage baking into .tox**: `onProjectPreSave` now unstores `_git_root`, `_tdn_stripped_paths`, and `_tdn_pane_restore` — these are session-only values that caused spurious warnings (e.g. "Post-save restore: .tdn file missing: unit_tests.tdn") when baked into the release .tox
- **Fix: parexec SyntaxError on save**: Fixed non-ASCII bytes (smart quotes, em dashes) in parexec.py that caused `SyntaxError` when TD reads externalized files with CP1252 encoding
- **Improved: `_restoreSettings()` kick_envoy parameter**: `onStart()` passes `kick_envoy=True` to defer Envoy start after settings restore; `Verify()` (onCreate path) uses default `kick_envoy=False` since it owns the Envoy startup flow
- **Test: Duplicate handling tests**: 5 new tests in `test_duplicate_handling.py` covering `_buildPathGroups`, `_resolveClonesByCloningAPI`, group dialog, and user-selects-master flow
- **Test: Smoke release fix**: `test_envoy_server_running_if_enabled` now checks `Envoystatus` parameter (survives extension reinit) instead of `envoy_running` store
- **Docs**: Updated duplicate path handling section in `externalization.md` (39 test suites, 1390 tests)

## v5.0.278

Fix folder change crash, regression tests.

- **Fix: Changing externalization folder deletes target directory** (GitHub issue #3): When changing the Folder parameter, `Disable()` would fall back to `project.folder` when the previous folder was empty, then `deleteEmptyDirectories` would walk the entire project tree and delete the newly-created target directory. `UpdateHandler` then failed with `FileNotFoundError`. Fixed by guarding all directory cleanup to never operate on `project.folder`, and switching `os.mkdir` to `os.makedirs(exist_ok=True)` for robustness
- **Regression tests**: Two new tests in `test_custom_parameters.py` — `test_zz_folder_10_empty_dir_survives_disable` reproduces the exact issue #3 scenario, `test_zz_folder_11_disable_empty_prev_skips_project_folder` verifies empty prevFolder doesn't walk project.folder (39 test suites)

## v5.0.277

Manager UI improvements, new keyboard shortcut, consistent terminology.

- **"Update current COMP" toolbar button**: New button (floppy disk icon) in the toolbar directly after "Update externalizations", calls `SaveCurrentComp()` — equivalent to Ctrl+Alt+U. Visible in both full and minimized manager views
- **Ctrl+Shift+R keyboard shortcut**: New shortcut to refresh tracking state, added to keyboard callbacks, toolbar tooltip, and all documentation
- **Consistent "Update" terminology**: Replaced mixed "Save"/"Update" language across all user-facing text — tooltips, help text, docs, and README now consistently use "Update" for externalization operations (Ctrl+Shift+U, Ctrl+Alt+U)
- **Minimized UI fix**: Reduced `min_height` from 72 to 66 to eliminate black bar at bottom of minimized manager (header 26px + toolbar 40px = 66px exactly). Increased `min_width` from 370 to 410 to accommodate the new button
- **Manager list default expand**: Root-level items in the externalization list now start expanded on first launch instead of fully collapsed
- **Restored unit_tests annotations**: 6 annotation groups accidentally removed in v5.0.269 commit (irony: the "fix annotation loss" commit) have been restored from git history
- **TDN reload rule**: CLAUDE.md rule #1 strengthened — editing `.tdn` files on disk now mandates an immediate `import_network` MCP call to reload in TD
- **Manager toolbar docs**: New toolbar button reference table added to `manager-ui.md` with all buttons, actions, and keyboard shortcuts

## v5.0.275

TDN export keyboard shortcut pars, keyboard shortcuts documentation.

- **TDN export shortcut pars**: Added `Export Project to TDN` and `Export Current COMP to TDN` read-only parameters to the UI custom page, displaying the `ctrl/cmd + lshift + e` and `ctrl/cmd + alt + e` shortcuts alongside the existing four shortcut pars
- **Keyboard shortcuts docs**: Added an info callout to `keyboard-shortcuts.md` clearly explaining the difference between Save shortcuts (update tracked externalizations) and Export shortcuts (standalone TDN snapshot of any network)

## v5.0.274

Settings persistence across upgrades, extension initialization timing documentation.

- **Settings persistence (`.embody.json`)**: Embody now saves user-configured parameters to a `.embody.json` file at the git root (or project folder if no git). Settings are written automatically on every parameter change and restored on project open (`onStart`) and fresh install (`onCreate`). Survives `.tox` upgrades, crashes, and force-quits. Whitelisted parameters include folder, Envoy config, tag names, tag colors, TDN settings, and logging options. Restore runs silently (no `onValueChange` side effects) via `_restoring_settings` flag
- **Crash-safe restore**: `_restoreSettings()` runs at frame 5 on every project open, not just on fresh install. If the `.toe` has stale values (unsaved session, crash), `.embody.json` wins
- **Extension initialization timing docs**: New documentation covering the critical `onInitTD` / TDN import timing issue — extensions inside TDN COMPs must defer initialization because `ImportNetwork(clear_first=True)` overwrites any state set during `onInitTD`. Added to `td-python.md` rule, `create-extension` skill, `extensions.md` doc, and TDN specification
- **Template sync**: Updated `text_rule_td_python.md` and `text_skill_create_extension.md` templates to match their `.claude/` counterparts

## v5.0.269

Fix annotation loss on save, TDN v1.2, poisoned zero value guards, bridge improvements.

- **Fix TDN annotation loss on Ctrl+S**: Two import-path bugs caused annotations to disappear after save. Phase 2 (`_createCustomPars`) called `appendXXX(replace=True)` on palette clone operators (annotateCOMP), destroying internal parameter bindings that the clone's rendering network depends on — fix: skip Phase 2 for `palette_clone` operators. Phase 1 (`_createOps`) only logged a warning when TD ignored the name param for annotateCOMP creates, causing Phase 7a to create duplicates — fix: explicitly rename after creation
- **Guard annotation import/export against poisoned zero values**: Previous palette clone bug exported `titleHeight=0`, `bodyFontSize=0`, `backAlpha=0.0` from broken annotations, making them invisible on reimport. Both import and export now skip zero values, letting palette clone defaults apply
- **TDN v1.2**: Storage options, `tdn_ref` cross-validation, large TDN warning
- **Envoy bridge improvements**: Signal diagnostics, startup log improvements
- **Toolbar/UI updates**: Press state improvements, button interactions
- **New test coverage**: `test_tdn_file_io.py` added for TDN file I/O operations

## v5.0.263

DAT content safety, palette clone fidelity, recursive TDN fingerprinting, toolbar press states, venv validation.

- **DAT content safety**: Pre-save check detects unexternalized DATs inside TDN COMPs that would lose content during the strip/restore cycle. Prompts with Externalize / Skip / Always Externalize / Never Ask options. New `Tdndatsafety` parameter stores the user's preference. Called from `onProjectPreSave()` before TDN export
- **Palette clone parameter fidelity**: TDN export now compares parameters against both `p.default` and the clone source's actual value. Parameters that match `p.default` but differ from the clone source are preserved, fixing silent data loss on rebuild (e.g., `buttontype` defaulting to `"momentary"` when clone source is `"toggledown"`). `clone`/`enablecloning` parameters are excluded from export — TD auto-sets these
- **Recursive TDN fingerprinting**: `_computeTDNFingerprint()` now recurses into child COMPs that don't have their own TDN externalization, so edits deep inside nested COMPs (e.g., editing a POP inside a geometryCOMP) trigger the parent's dirty detection
- **Toolbar and window header press states**: Buttons now show a pressed visual on mousedown and restore hover on release, providing immediate click feedback
- **Manager list selection persistence**: Selected row is tracked by operator path and survives list refreshes and reorders
- **Envoy venv validation**: `EnvoyExt` now validates that the `.venv` Python actually executes before using it for the bridge. Catches stale `pyvenv.cfg` pointing to uninstalled TD versions and falls back to system Python with a warning
- **Bridge Python logging**: Bridge now logs the Python executable path and version at startup for diagnostics
- **`Envoyinstancename` parameter removed**: Auto-suffixed instance naming (`MyProject`, `MyProject-2`) is the sole mechanism. References removed from docs and skills
- **Documentation updates**: New DAT Content Safety section in externalization docs, Broken Virtual Environment troubleshooting, expanded palette clone and fingerprint documentation in TDN specification, removed stale `Envoyinstancename` references across 5 docs
- **New tests**: 12 palette clone round-trip fidelity tests (Section V in `test_tdn_reconstruction.py`). 39 test suites total

## v5.0.260

Bridge stability: signal diagnostics, conditional bridge-script writes, connectivity wording fix.

- **Bridge signal diagnostics**: `envoy_bridge.py` now installs SIGTERM/SIGINT handlers that log PID, current parent PID, and original parent PID before exiting. Startup log messages also include PID/PPID. Helps diagnose what process kills the bridge (Claude Code file watcher, orphan reaping, etc.)
- **Conditional bridge-script write**: `EnvoyExt._configureMCPClient()` now compares bridge script content before writing. If unchanged, the file is not rewritten — preventing Claude Code's file watcher from restarting the MCP server mid-connection
- **Connectivity rule wording**: Updated recovery step 3 from "close this tab/session and reopen a fresh one" to "reopen this session/conversation" for clarity

## v5.0.259

Mandatory operator layout rules, `/local` path prohibition, TD connectivity recovery rule.

- **Mandatory operator positioning**: The create-operator workflow now requires explicit `set_op_position` for every operator created via MCP. Auto-placement is no longer acceptable — agents must batch-compute grid-aligned positions before creating operators, verify layout afterward, and ensure left-to-right signal flow. Previously, positioning was documented as optional ("reposition if needed"), which led to messy, unreadable networks
- **`/local` path prohibition**: New critical rule (#3 in CLAUDE.md) and step 1 in the create-operator workflow: agents must NEVER create operators under `/local` or `/local/*`. The `/local` storage is volatile and not saved with the `.toe` file. Agents must place operators under the project root or use `ui.panes.current.owner.path` to find the active network
- **TD connectivity recovery rule**: New always-loaded rule (`td-connectivity.md`) with session-start verification, recovery procedures for lost MCP tools, and fix sequences for stale `.envoy.json` entries, stuck bridges, and dead TD instances

## v5.0.258

Multi-instance Envoy support, auto-suffix collision avoidance, `switch_instance` bridge meta-tool.

- **Multi-instance port allocation**: Envoy now scans a 10-port range (`base` through `base+9`) when the preferred port is occupied by another instance. Each TD instance gets its own port automatically — up to 10 simultaneous instances per base port
- **Instance registry collision avoidance**: `_instanceKey()` now checks PID liveness before reusing a registry key. When the same `.toe` file is opened in multiple instances, keys are auto-suffixed (`MyProject`, `MyProject-2`, etc.). Stale entries with dead PIDs are reclaimed automatically
- **`Envoyinstancename` parameter**: Optional custom name for the Envoy instance registry. Overrides the auto-generated key from the `.toe` filename — useful for predictable `switch_instance` targets
- **`switch_instance` bridge meta-tool**: List all registered TD instances or switch the bridge to a different running instance. Redirects the bridge's HTTP target in-memory for instant switching with no restart
- **`_findAvailablePort()` refactor**: Extracted port-scanning logic from `Start()` into a dedicated method. Replaces the recursive retry loop with a clean single-pass scan
- **Atomic JSON writes**: New `_atomicWriteJSON()` method for `.envoy.json` writes — uses temp file + `os.replace()` with Windows `PermissionError` retry to prevent corruption under concurrent access
- **Graceful shutdown via MCP**: Documented `project.quit()` as the preferred way to close TD instances programmatically — triggers `onDestroyTD` for clean deregistration
- **Multi-instance documentation**: New `/multi-instance` skill, updated architecture docs, setup guide, Claude Code integration docs, troubleshooting entries, and tools reference. All surfaces document `switch_instance`, port allocation, instance registry, and same-project behavior

## v5.0.252

Windows process-kill fix, reconstruction verification fix.

- **Windows `is_process_alive()` fix**: `os.kill(pid, 0)` on Windows calls `TerminateProcess()`, killing TouchDesigner instead of checking liveness. Every `get_td_status`, `launch_td`, and `restart_td` call terminated TD on Windows. Now uses `OpenProcess(SYNCHRONIZE)` via ctypes on Windows, preserving the Unix signal-0 path for macOS/Linux
- **Reconstruction verification fix**: `_verifyReconstructedComp()` accessed `child.errors` and `child.warnings` as properties instead of calling them as methods (`child.errors()`, `child.warnings()`). This caused `'builtin_function_or_method' object has no attribute 'split'` warnings on every TDN reconstruction — error and warning checking was silently skipped
- **New tests**: 2 Windows `is_process_alive` tests (mocked OpenProcess for live and dead PIDs). 39 test suites total

## v5.0.251

Nested TDN child-skip on import, depth-sorted reconstruction ordering, material reference fix.

- **Nested TDN child-skip during import**: When a parent TDN contains children for a child COMP that has its own TDN externalization entry, the child's `children` array is now skipped during import. The child COMP shell is still created, but its internal network is left to its own `.tdn` file — preventing stale parent snapshots from overwriting updated child networks. New `_getTDNExternalizedPaths()` and `_stripNestedTDNChildren()` helper methods handle detection and recursive stripping
- **Depth-sorted TDN reconstruction**: `_getTDNStrategyComps()` now sorts entries by path depth (fewest segments first), ensuring parents are always imported before their children during project-open reconstruction. Combined with the child-skip logic, each COMP's network is populated exactly once from its authoritative `.tdn` file
- **Import input validation**: `ImportNetwork()` now validates that `operators` is a list, returning a clear error instead of failing cryptically on malformed input
- **Material reference test fix**: Corrected `test_T07_geometry_material_roundtrip` to use `./my_mat` (child reference) instead of `my_mat` (sibling reference), which was unresolvable from inside the geometryCOMP
- **`assertAlmostEqual` added to test framework**: TestRunnerExt now supports `assertAlmostEqual(first, second, places=7, delta=None)` for floating-point comparisons
- **TDN spec updated**: New "Nested TDN-Externalized COMPs" section documents the child-skip behavior, import/export semantics, and reconstruction ordering
- **Externalizations table cleanup**: Removed stale test entries (tdn_geo_test, tdn_deep, etc.) from tracking table
- **New tests**: 4 nested TDN child-skip tests (Section U: skip children of TDN-externalized COMPs, import non-TDN children normally, depth sorting verification, deeply nested skip). 39 test suites total

## v5.0.247

Default-child cleanup on TDN import, nested TDN save-cycle fix, SOP-to-COMP connection hardening.

- **Clear auto-created defaults on COMP creation during import**: When TDN import creates a COMP (e.g. geometryCOMP) that has inline children defined, auto-created default children (e.g. Torus POP) are now destroyed before recursing into the TDN children. Previously, default children persisted alongside imported ones because they were filtered out during export (`_TRIVIAL_KEYS`) and never visited during import. Verified at 10 levels of nesting depth
- **Nested TDN strip/restore ordering**: Save cycle now strips deepest-first and restores shallowest-first. Previously, stripping a parent TDN COMP destroyed nested TDN COMPs before they could be tracked, so post-save restore never rebuilt them — leaving default children instead of the correct TDN contents
- **SOP-to-COMP connection fallback**: `_wireConnectionList` now bounds-checks `inputConnectors` before indexing and falls back to `inputCOMPConnectors` for COMPs that accept SOP/TOP/CHOP wire inputs where connectors may not be populated immediately after creation

## v5.0.243

Headless smoke testing, file cleanup preferences, specialized COMP support, portable .tox hardening, bridge project_path override.

- **`_messageBox` auto-response system**: Dialog calls can be intercepted by seeding `_smoke_test_responses` in storage, enabling fully headless smoke testing of Embody's init sequence including Envoy opt-in and re-scan prompts. Responses are consumed on use
- **File cleanup preference**: New `Filecleanup` parameter (ask/keep/delete) controls whether external files are deleted when un-tagging operators. "Always Keep" and "Always Delete" options persist the choice
- **TDN default child filtering**: Uncustomized auto-created children (e.g. `torus1` inside a geometryCOMP) are now skipped during export — they carry only trivial keys (name, type, position, size) and TD recreates them on COMP creation
- **Portable .tox export hardening**: `ExportPortableTox` now strips the target COMP's own `externaltox`/`enableexternaltox` params (not just descendants) and handles the `syncfile` parameter, preventing baked-in references from confusing recipients
- **Bridge `project_path` override**: `launch_td` and `restart_td` meta-tools accept an optional `project_path` parameter to open a different `.toe` file, resolved relative to the git root
- **Envoy start deferred**: `parexec.py` defers `Start()` by 5 frames so `onCreate` has time to suppress baked-in `Envoyenable=True` before the server launches
- **SCM directory protection**: `deleteEmptyDirectories` and `_cleanupFolder` now skip `.git`, `.svn`, and `.hg` directories
- **Cross-platform temp paths**: All Envoy temp file operations use `tempfile.gettempdir()` instead of hardcoded `/tmp`
- **`findChildren()` fix**: Two calls using invalid `depth=-1` corrected to `findChildren()` (unlimited depth is the default)
- **AGENTS.md rewrite**: Condensed from verbose rule duplication into a concise universal AI instructions file
- **ENVOY.md updated**: TDN-first rule added, skill prerequisites section, verify-TD-claims rule
- **Release smoke test infrastructure**: Bootstrap script (`smoke_bootstrap.py`) and template `.toe` for E2E release testing
- **New tests**: 22 smoke release tests (post-init state, `_messageBox` mechanism, `_promptEnvoy` auto-response, Envoy state), 9 specialized COMP roundtrip tests (geometryCOMP children, flags, materials, strip/restore; cameraCOMP; lightCOMP). 39 test suites total

## v5.0.237

TDN v1.1 format with target COMP metadata, import error surfacing, MCP permissions documentation, save-cycle pane restoration, git init error dialog, Envoy troubleshooting docs.

- **TDN v1.1 format**: Exports now include the target COMP's `type`, `flags`, `color`, `tags`, `comment`, and `storage` at the top level. On import, type mismatches produce a warning. Existing v1.0 files remain fully importable
- **Locked non-DAT operator warning**: Export and import now detect locked TOPs, CHOPs, and SOPs and warn that their frozen data won't survive a TDN round-trip. Documented in spec and externalization docs
- **Import error surfacing**: `ImportNetwork()` and `ImportNetworkFromFile()` now set `ui.status` on failure, so TD users see errors in the status bar — not just in logs or MCP responses
- **MCP auto-authorization documented**: The Envoy enable dialog now informs users that all MCP tools are auto-authorized and points to `.claude/settings.local.json` for adjustments. New "MCP Tool Permissions" section added to Envoy setup docs
- **Save-cycle pane restoration**: When TDN strip/restore runs during project save, pane owners inside TDN COMPs are now saved before stripping and restored after import — no more orphaned panes
- **Git init error dialog**: If `git init` fails during Envoy setup, a `ui.messageBox` now shows the error and manual fix instructions instead of silently falling through
- **Envoy troubleshooting docs**: New troubleshooting page covering server startup failures, connection issues, git init problems, and log file locations
- **Dialog sequencing fix**: The Envoy opt-in prompt now waits for all other init dialogs (deprecated patterns, re-scan) to resolve before appearing
- **TDN reconstruction uses type from file**: `ReconstructTDNComps()` now reads the `type` field from v1.1 `.tdn` files when creating missing COMP shells, so the correct COMP type (geometryCOMP, containerCOMP, etc.) is used instead of defaulting to baseCOMP
- **New tests**: Locked non-DAT warning test, target COMP metadata preservation tests (6 tests for type, flags, color, tags, comment, storage round-trips)

## v5.0.235

`restart_td` bridge meta-tool, local MCP handshake when TD is down, operator overlap warnings, layout rules hardening.

- **`restart_td` bridge meta-tool**: Gracefully quits TouchDesigner and relaunches with the project's `.toe` file. Sends platform-appropriate quit signal, waits for exit (force-kills if needed), then relaunches and waits for Envoy. Crash-loop aware — respects the existing 3-in-5-minutes limit
- **Local MCP handshake when TD is down**: The STDIO bridge now handles `initialize`, `notifications/initialized`, and `tools/list` locally when Envoy is unreachable, so Claude Code always completes the MCP setup and discovers bridge meta-tools without waiting for a connection timeout
- **`set_op_position` overlap warning**: After repositioning an operator, EnvoyExt checks for bounding-box overlaps with siblings (20-unit margin) and returns an `overlap_warning` field naming the conflicting operators
- **Layout rules hardening**: Network-layout and create-operator rules now require dimension-aware spacing (`nodeWidth`/`nodeHeight` from `get_network_layout`), forward-flow wire direction, and flag the fixed-offset anti-pattern. OP-reference parameter values section added to parameter rules
- **Bridge meta-tools documented**: Architecture, setup, claude-code, and tools-reference docs updated with STDIO bridge section, `.envoy.json` config reference, and meta-tool catalog

## v5.0.233

Project-level performance monitoring, pre-handoff validation, Envoy bridge hardening, test runner dialog fix.

- **`get_project_performance` MCP tool**: Reads a permanent Perform CHOP inside Embody to report FPS, frame time, GPU/CPU memory, dropped frames, active ops, GPU temperature, and optional COMP hotspot ranking by cook time
- **`/validate` command**: Pre-handoff checklist that snapshots performance, scans for errors, checks externalization health, evaluates thresholds, and reports a PASS/WARN/FAIL verdict with hotspot analysis
- **Test runner dialog fix**: `Filecleanup` parameter is now suppressed to `delete` during test runs (save/restore across all entry points), preventing modal "Removed Operator Detected" dialogs from blocking test execution
- **Continuity check sandbox filtering**: Path-based filtering for test sandbox operators as a second safety layer — sandbox ops are silently filtered even when the `_running` flag isn't active (handles reinit, between-suite gaps, post-failure)
- **Envoy bridge hardening**: `.envoy.json` project config for bridge launcher, venv Python preference over system Python, stale process cleanup with orphan watchdog

## v5.0.229

Warning support in `get_op_errors`, Envoy enable dialog improvement, cleanup.

- **`get_op_errors` now returns warnings**: The MCP tool calls both `OP.errors()` and `OP.warnings()`, returning structured `warnings`/`warningCount`/`hasWarnings` fields alongside existing error data. Cook dependency loops and other TD warnings are now surfaced to AI clients
- **Envoy enable dialog note**: The first-run dialog now mentions that TD will be briefly unresponsive during dependency installation
- **Cleanup**: Removed stale `base_tox.tdn` and test externalization entries from tracking table

## v5.0.228

macOS timezone fix, toolbar hover highlight.

- **macOS timezone abbreviation fix**: Local timestamp display in the manager list now shortens verbose macOS timezone names (e.g. "Pacific Daylight Time" → "PDT") by extracting initials
- **Toolbar hover highlight**: Container right toolbar button background color now uses an expression to brighten on hover

## v5.0.227

TDN crash safety, atomic writes, content-equal skip, About page filtering.

- **Atomic TDN writes**: `TDNExt._safe_write_tdn` now writes via temp file + `os.replace` + `fsync` to prevent partial writes corrupting `.tdn` files on crash or power loss
- **Backup rotation**: Before each write, `.tdn` files are copied to `.tdn_backup/` (`.bak` and `.bak2` generations). `.tdn_backup/` is git-ignored
- **Post-write validation**: After each atomic write, the file is read back and parsed. If validation fails, the previous backup is automatically restored
- **Rollback on reconstruction failure**: `ReconstructTDNComps` and `onProjectPostSave` now attempt rollback from `.bak` if reconstruction fails after import
- **Content-equal skip**: Pre-save export compares new TDN content against the existing file (ignoring volatile header fields: `build`, `generator`, `td_build`, `exported_at`). Unchanged COMPs are skipped, eliminating noisy git diffs
- **Structural dirty detection**: `Refresh` now detects structural changes in TDN-strategy COMPs (not just parameter changes) and triggers `SaveTDN` when children are added/removed/renamed
- **About page filtering**: `Build`, `Date`, and `Touchbuild` parameters are excluded from TDN export and reconstructed from `externalizations.tsv` at import time via `_reconstructAboutPage`. Prevents version metadata from polluting TDN diffs
- **Continuity dialog suppression**: File cleanup dialog is suppressed when the test runner is active, preventing modal spam during rapid operator create/destroy cycles
- **Continuity check fix**: Individually-externalized children are only skipped if the parent TDN COMP is completely absent (crash recovery). If the parent exists but is empty, genuine deletions are detected normally
- **Rules frontmatter strip**: `_writeTemplate` now strips YAML frontmatter before writing rules to user projects (Claude Code doesn't read frontmatter in `.claude/rules/`)
- **New test suite**: `test_tdn_crash_safety.py` — atomic write behavior, backup rotation, post-write validation, failure injection, and stress tests (37 total suites)
- **Expanded test coverage**: `test_tdn_helpers.py` adds `_tdn_content_equal` and `_read_existing_tdn` tests; `test_tdn_reconstruction.py` adds S-series About page filtering tests

## v5.0.222

Rename `tag_for_externalization` to `externalize_op`, clarify single-step workflow.

- **MCP tool rename**: `tag_for_externalization` → `externalize_op` across EnvoyExt, docs, skills, templates, and settings. The new name better reflects that the tool tags AND writes to disk in one step
- **Externalize workflow clarification**: Skill and docs now explain that `externalize_op` is a single-step operation (no separate `save_externalization` needed), and that `save_externalization` is for re-exporting already-externalized operators
- **Test updates**: Renamed test methods and references to match new tool name

## v5.0.221

TDN annotation properties, GitHub release rule, templates cleanup.

- **TDN annotation properties**: Export and import now support `backAlpha`, `titleHeight`, and `bodyFontSize` annotation parameters, preserving non-default values through TDN round-trips
- **GitHub release rule**: New `.claude/rules/github-release.md` with post-push workflow for detecting release artifacts, extracting version from changelog, and creating GitHub releases via `gh` CLI. Added to `EmbodyExt._TEMPLATE_MAP_RULES` for auto-deployment, template synced, release-commits.md updated with mapping
- **Templates TDN cleanup**: Annotations in `templates.tdn` now use the native annotation format instead of being represented as annotateCOMP operators. Removed `annotateCOMP` type defaults and `par_templates` section. Expanded Rule Templates annotation to accommodate new template DAT

## v5.0.220

Network layout rule rewrite, commit-push checklist, expanded settings template, tooltip fix.

- **Network layout rule rewrite**: Replaced verbose placement rules with a concise 7-step placement procedure, added anti-patterns section and complexity thresholds for when to encapsulate into COMPs. Template synced
- **Commit-push checklist rule**: New `.claude/rules/commit-push-checklist.md` enforcing change evaluation, doc audit, test audit, and release detection before every commit. Added to `EmbodyExt._TEMPLATE_MAP_RULES` for auto-deployment, template synced, release-commits.md updated with mapping
- **Expanded MCP tool allowlist**: Settings template (`text_settings_local.json`) now includes all 42 MCP tools sorted alphabetically, instead of only read-only tools
- **Tooltip fix**: Toolbar tooltip text changed from "Refresh tracking state" to "Clear filter" with repositioned widget
- **Parameters template BOM fix**: Restored missing BOM marker on `text_rule_parameters.md`

## v5.0.217

TDN target COMP parameter preservation, user-prompted file cleanup, dock safety, companion reuse fix, git init hardening.

- **Target COMP parameter preservation**: TDN export now captures the target COMP's own custom parameters (`custom_pars`) and non-default built-in parameters (`parameters`) at the root level of the TDN document. Import restores these in a new Phase 9 after child creation, so extension reinit doesn't clobber custom par values. 5 new tests cover roundtrip survival of custom pars, expressions, built-in params, bare shell creation, and backward compatibility
- **Help text in TDN**: Custom parameter definitions now export and import `help` tooltip text. TDN schema and specification updated with the new `help` field
- **User-prompted file cleanup**: When the continuity check detects externalized operators removed from the network whose backing files still exist on disk, Embody now prompts the user to keep or delete the files instead of silently skipping. Supports "Always Keep" / "Always Delete" persistent preferences via the new `Filecleanup` parameter
- **Dock safety on destroy**: Both `_clearChildren` (EmbodyExt) and TDN import (`clear_first`) now clear `child.dock = None` before destroying child operators, preventing uncatchable `tdError` when a dock target is destroyed before its docked operator
- **Companion reuse fix**: `_createOps` now tracks pre-existing operator names to distinguish them from auto-created companions during merge imports. Prevents merge (non-`clear_first`) imports from incorrectly reusing operators that existed before import started
- **Git init hardening**: `_ensureGitRepo` strips `GIT_DIR`, `GIT_WORK_TREE`, and other git env vars before `git init` to prevent broken repos caused by TD's embedded Python environment. Verifies the init with `git rev-parse` and retries on failure
- **`attrs<25` version pin**: MCP dependency install now pins `attrs<25` to avoid conflicts with TD's bundled `attr` module. Startup detects and downgrades attrs 25.x automatically
- **Tagger refactoring**: Extracted `_removeExternalization` (no-dialog removal) and `_dispatchTaggerButton` (label-based routing for manage-mode buttons) from inline handler code
- **`RemoveListerRow` / `_removeTDNStrategy`**: Now accept `delete_file` parameter to optionally preserve files on disk when removing tracking entries
- **Parameter rules**: New dedicated `.claude/rules/parameters.md` covering help text, sections, naming, ranges, styles, and page organization. `td-python.md` now points to it. TD API reference skill updated with post-creation property examples
- **New tests**: `test_strategy_handlers.py` with 15 tests covering `_removeExternalization`, `HandleStrategySwitch`, `_dispatchTaggerButton`, and manage-mode button dispatch. `test_mcp_externalization.py` gains tearDown cleanup for sandbox entries

## v5.0.210

DAT restoration on startup, continuity check hardening, manager list row limiting.

- **Automatic DAT restoration**: New `RestoreDATs()` method recreates missing DAT-strategy operators from externalized files on project open (frame 50). Controlled by `Datrestoreonstart` parameter. Safely excludes Embody descendants and DATs inside TOX/TDN COMPs
- **Continuity check hardening**: Before removing entries for missing operators, checks if the backing file exists on disk — recoverable entries are preserved for restoration instead of being deleted
- **Manager list row limiting**: Tree starts collapsed by default. LRU-based auto-collapse keeps visible rows under 100, protecting the active branch from being collapsed
- **TDN structural cleanup**: toolbar.tdn and tagger.tdn shed embedded child definitions in favor of externalized `.tdn` files — smaller diffs, cleaner hierarchy
- **base_test converted to TDN**: Replaced binary `base_test.tox` with `base_test.tdn` for diffability
- **text_claude.md relocated**: Template moved from `Embody/` root into `Embody/templates/` alongside other template DATs
- **New tests**: `test_dat_restoration.py` with 13 tests covering DAT restoration, skip conditions, and continuity check recovery

## v5.0.208

Settings auto-deploy, bridge template, Envoy startup resilience.

- **settings.local.json auto-deploy**: Read-only MCP tool permissions deployed automatically on Envoy startup
- **Bridge script template**: `text_envoy_bridge.py` and settings template moved into the templates COMP for centralized management
- **Envoy startup resilience**: `_upgradeEnvoy()` failure no longer blocks MCP server startup
- **`.gitignore` managed entries**: Expanded auto-managed entries to include `Backup/`, `logs/`, `CrashAutoSave*`

## v5.0.207

Claude Code integration docs, slash commands, CLAUDE.md deduplication.

- **Claude Code Integration docs**: New [documentation page](envoy/claude-code.md) covering the generated `.claude/` directory — rules, skills, slash commands, and customization
- **Slash commands**: Added `/run-tests`, `/status`, and `/explore-network` commands to `.claude/commands/` for common workflows
- **CLAUDE.md deduplication**: Moved rules that were restated in both `CLAUDE.md` and `.claude/rules/` into rules only — reduced critical rules from 15 to 9. Skill prerequisites moved to dedicated `skill-prerequisites.md` rule
- **Getting Started update**: `.gitignore` documentation updated to reflect specific `.claude/` entries instead of blanket directory exclusion

## v5.0.206

Metadata reconciliation, network layout tool, save_externalization fix.

- **Metadata reconciliation**: New `ReconcileMetadata()` method runs at frame 75 on project open — re-applies tags, colors, file parameters, and readOnly flags to operators that exist in the externalizations table but lost their in-memory metadata (e.g. when TD was closed without saving after tagging)
- **`get_network_layout` MCP tool**: Returns positions and sizes of all operators and annotations in a COMP in a single call — replaces the need for repeated `get_op_position` calls. Includes bounding box calculation
- **`save_externalization` fix**: Now correctly handles TDN-strategy COMPs (calls `SaveTDN`) and file-synced DATs, instead of blindly calling `Save()` which only works for TOX-strategy COMPs
- **`Save()` guard**: Validates target is a COMP before proceeding — prevents cryptic errors on non-COMP operators

## v5.0.205

Fix companion DAT duplication during TDN strip/restore save cycle.

- **Companion DAT reuse on import**: `_createOps` now detects auto-created companion DATs (timerCHOP callbacks, rampTOP keys, etc.) and reuses them instead of creating duplicates that accumulate on each save
- **Duplicate companion cleanup on export**: `_exportChildren` detects and skips accumulated companion duplicates (e.g. `timer1_callbacks1`, `timer1_callbacks2`) using docking-based detection — existing `.tdn` files self-clean on next save

## v5.0.204

Custom window header, path portability, TDN template cleanup.

- **Custom window header**: Replaced `widgetCOMP` clone of TDBasicWidgets with a lightweight `containerCOMP` + `WindowHeaderExt` extension — minimize/maximize/close with hover-based button detection, no palette dependency
- **Absolute path elimination**: Replaced hardcoded `/embody/...` paths with relative expressions (`=op('container_left')`, `=me.op('externalizations')`, etc.) in toolbar and root TDN files for full portability
- **TDN template cleanup**: Removed unused `type_defaults` entries (baseCOMP, panelexecuteDAT, constantTOP, opexecuteDAT) and stale custom par pages (settings_2, expressions) from Embody.tdn — smaller, cleaner exports
- **EmbodyExt.py**: `self.ownerComp.path` → `self.my.path` for consistency with codebase conventions

## v5.0.203

Multi-client AI config, TDN docking, robust init.

## v5.0.201

Robust first-install init, table schema expansion, release build hardening.

- **Automatic init on drop**: `onCreate()` now disables Envoy before the table exists (prevents premature git-root detection), creates the externalizations table at frame 15, then runs `Verify()` at frame 30 — all fully async and idempotent
- **`CreateExternalizationsTable()` (new public method)**: Safe to call at any time. No-op if the table is already connected; reconnects to a surviving sibling after an upgrade without duplicating; creates fresh only when truly absent. Also wired to the **Create Externalizations Table** pulse parameter
- **`Verify()` — two-scenario detection**: Fresh install (empty table) runs `UpdateHandler` quietly and offers Envoy opt-in. Upgrade (table has prior data) prompts a re-scan dialog before offering Envoy opt-in
- **Externalizations table schema**: Added `strategy`, `node_x`, `node_y`, and `node_color` columns. Existing tables are migrated automatically on first open
- **Release build**: `execute_src_ctrl.py` now clears `Tdnfile` and `Networkpath` pars before `ExportPortableTox()` so the baked `.tox` doesn't carry stale TDN paths into new projects

## v5.0.190

Automatic restoration, documentation overhaul.

- **Automatic restoration**: TOX-strategy COMPs are restored from `.tox` files and TDN-strategy COMPs are reconstructed from `.tdn` files on project open — users no longer need to save their `.toe` to preserve externalized work
- **Documentation overhaul**: Updated all documentation (README, docs site, help text, CLAUDE.md, text_claude.md) to reflect that externalized files on disk are the source of truth, removing outdated `ctrl+s` save workflow references

## v5.0.178

Reload from disk, full project TDN safety, continuity hardening.

## v5.0.171

Export Portable Tox, improved tag management, TDN error handling, window management refactor.

- **Export Portable Tox**: New `ExportPortableTox()` method exports any COMP as a self-contained `.tox` with all external file references and Embody tags stripped. Available from the Manager UI Actions menu and used automatically for release builds
- **Improved tag stripping**: Disable now sweeps all project operators for stale Embody tags, not just tracked ones
- **TDN error handling**: `ImportNetworkFromFile` now returns structured error dicts instead of `None` on failure
- **TDN per-COMP split**: Refactored `_splitPerComp` into a reusable static method
- **Window management**: Tagging menu and manager UI refactored into standalone window COMPs (`window_tagging_menu`, `window_manager`)
- **Keyboard shortcut update**: `lctrl-lctrl` now shows an Actions menu for already-tagged operators (tag, retag, export portable tox, etc.)
- **Release build**: `execute_src_ctrl.py` now uses `ExportPortableTox()` instead of raw `comp.save()` for portable release `.tox` files
- **Test fixes**: Updated test_custom_parameters (synchronous `ReexportAllTDNs` call, Envoy transitional state handling) and test_tdn_reconstruction (improved continuity check distinguishing pure TDN children from individually-externalized ones)

## v5.0.163

Re-export TDN files for list, manager, and container_right after param changes.

## v5.0.140

TDN strip/restore hardening, `file`/`syncfile` export, post-import validation, TDN restore UI, companion DAT reuse during import, bug fixes.

- Save-in-progress guard blocks mutating MCP operations during the strip/restore save window
- Pre-save verifies `.tdn` file exists before stripping children (prevents data loss)
- Post-save tracks restore failures for retry on next project open
- `file` and `syncfile` parameters now exported in TDN for self-contained externalized DAT round-trips
- New "Restore from TDN" button in tagger actions menu for TDN-strategy COMPs
- Post-import validation checks for missing file references and cook errors
- Companion DATs (auto-created by rampTOP, timerCHOP, etc.) are reused during import instead of creating duplicates
- Component-level TDN files protected from stale-file cleanup during project-level exports
- `StripCompChildren` now destroys annotations and respects Embody protection chain
- UI: midline ellipsis glyph for strategy column, active-menu state tracking, "Tag" label for unexternalized COMPs
- Fixed: `SaveDAT()` crash (undefined property), `_save_externalization` type mismatch, duplicate row corruption (missing strategy column)

## v5.0.130

TDN strategy externalization, strip/restore save cycle, compact TDN format.

- New externalization strategy: COMPs can use TDN (export/import) instead of TOX, enabling human-readable diffs
- Strip/restore save cycle: TDN-strategy COMP children are stripped before `.toe` save and reconstructed from `.tdn` on project open, keeping the `.toe` small
- Compact TDN format: `type_defaults` hoists shared parameter values, `par_templates` deduplicates custom parameter definitions, expression shorthand (`=` prefix for expressions, `~` for binds)
- Per-COMP split export mode: large networks export as one `.tdn` file per COMP for git-friendly directory structures
- `externalizations.tsv` gains `strategy` column (`tox`, `tdn`, `py`, `txt`, etc.)
- Continuity check skips TDN-strategy children (lifecycle managed by TDN, not individual externalization)
- 30 test suites covering all functionality

## v5.0.93

Modular sub-components, TDN snapshots, README rewrite.

- Embody UI refactored into externalized sub-components (toolbar, tagger, manager, window manager)
- TDN network snapshot support added
- README comprehensively rewritten with full feature documentation

## v5.0.86

Manager UI refactored into modular externalized components.

## v5.0.71

Rename Claudius to Envoy, expand README and help text.

## v5.0.61

Rename MCP tools for consistency, add auto-restart on port change, expand testing documentation.

## v5.0.59

Migrate tests to externalized DATs, add deferred test runner (one test per frame).

## v5.0.56

Rewrite test runner, fix `run()` safety, add 6 new test suites, update documentation.

## v5.0

Major release — Envoy MCP server, TDN format, comprehensive testing.

- **Envoy MCP Server**: 40+ tools for Claude Code integration
- **TDN Format**: export/import for operator networks
- **Test Framework**: 26 test suites with sandbox isolation
- **Structured Logging**: Multi-destination logging system
- **CLAUDE.md Auto-Generation**: Project context for AI assistants
- **Cross-platform**: macOS support

## v4.7.14

Safe file deletion — Embody now only deletes files it created. Untracked files preserved during disable/migration.

## v4.7.11

Cross-platform path handling (forward slashes on all platforms) + code cleanup.

## v4.7.6

Build save increment bug fix.

## v4.7.5

- ui.rolloverOp refactor
- Restore handling of drag-and-drop COMP auto-populated externaltox pars
- Cache parameters correctly between tox saves
- Parameter updated coloring for dirty buttons in UI
- Path lib implementation improvements
- Auto refresh on UI maximize
- Ignore untagged COMPs when checking for duplicate paths

## v4.6.4

- About page on externalized COMPs (Build Number, Touch Build, Build Date)
- Build/Touch Build in externalization table + Lister
- Window resizing support

## v4.5.23

- Fix deletion of old file storage after renaming
- Network cleanup, tagging optimization
- Fix duplicated rows from git merge conflicts

## v4.5.19

Allow master clones with clone pars to be externalized. Setup menu cleanup.

## v4.5.17

Bug fixes, smaller minimized window footprint.

## v4.5.2

- TSV support
- Clone tag for shared external paths
- Handle drag-and-dropped COMP externaltox pars
- Detect dirty COMP parameter changes

## v4.4.128

Support for COMPs with empty/error-prone clone expressions.

## v4.4.127

Textport warning for paused timeline.

## v4.4.126

Clean up Save and dirtyHandler methods, auto set enableexternaltox.

## v4.4.104

TreeLister, improved Tagger stability, color theme updates.

## v4.4.74

- Full project externalization
- Handle deletion and re-creation (redo) of COMPs/DATs
- Support renaming and moving COMPs/DATs

## v4.3.128

Fixed abs path bug, macOS Finder support, keyboard shortcuts.

## v4.3.122

Separated logic/data for easier Embody updates.

## v4.3.43

UTC timestamps, Save/Table DAT buttons, refactored tagging.

## v4.2.101

Fixed keyboard shortcut bug, updated to TouchDesigner 2023.

## v4.0.0

Support for various file formats, parameter improvements.

## v3.0.0

Initial release.
