# Changelog

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
