# Changelog

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
