# Changelog

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

- New externalization strategy: COMPs can use TDN (JSON export/import) instead of TOX, enabling human-readable diffs
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
- **TDN Format**: JSON export/import for operator networks
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
