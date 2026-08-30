---
description: "Embody-specific conventions for extensions, logging, file safety, and MCP tool development"
paths:
  - "dev/embody/**"
  - "CLAUDE.md"
---
# Embody Code Conventions

Embody dev only -- not in `_TEMPLATE_MAP_RULES`, so it has no shipped template counterpart and is never copied into user projects.
## Extension Naming
Extension classes and source DATs must follow the `NameExt` convention (e.g., `EmbodyExt`, `EnvoyExt`, `TDXNExt`, `TestRunnerExt`). Class name must match DAT name.

## Logging
Use `op.Embody.Log(message, level)` from anywhere. Levels: `'DEBUG'`, `'INFO'`, `'WARNING'`, `'ERROR'`, `'SUCCESS'`. Convenience methods: `op.Embody.Debug(msg)`, `.Info()`, `.Warn()`, `.Error()`. Logs go to FIFO DAT, textport, log file (`dev/logs/`), and ring buffer.

## File Safety
- Always use forward slashes (`/`) in file paths
- Only delete files tracked by Embody: `isTrackedFile()`, `safeDeleteFile()`
- Directory cleanup: `rmdir()` only (fails on non-empty) - never `shutil.rmtree()`
- `externalizations.tsv` is managed exclusively by Embody - NEVER edit directly

## Parameter Handling
- No `hasattr` for known parameters - Embody's custom pars are static and locked in the `.toe`
- Use them directly: `self.ownerComp.par.Envoystatus = 'Running'`

## MCP Tool Development
- **Error types**: (1) Protocol errors (the MCP SDK's MCPServer handles automatically), (2) Tool execution errors via `{'error': str(e)}` dicts
- **Input validation**: Validate all inputs before passing to TD. Check paths, verify operators exist, sanitize strings for `eval()`/`exec()`
- **Tool signatures are API contracts**: Changing parameter names, type hints, or docstrings in `_register_tools()` changes the public MCP interface
- **Localhost only**: `127.0.0.1`, never `0.0.0.0`

## Operator Management
- **Renaming**: Only rename the operator itself (via MCP `rename_op` or inside TD). NEVER rename externalized files on disk, NEVER manually update `file`/`externaltox`, NEVER edit the table. `checkOpsForContinuity` handles everything.
- **Creating Python files**: Always create the textDAT in TD first, then externalize via Embody. Never manually set `file`/`syncfile` parameters.
- **Never cache extension references**: Always call inline: `self.ownerComp.ext.Embody.Method()`. Cached refs go stale on reinit.

## File Editing Impact

| File | Impact | Notes |
|------|--------|-------|
| `EmbodyExt.py` | HIGH | Core engine. All externalization behavior. |
| `EnvoyExt.py` | HIGH | MCP server. Tool signature changes break API. |
| `TDXNExt.py` | MEDIUM | `.tdxn` format compatibility. |
| `execute.py` | LOW | Lifecycle callbacks. Rarely changes. |
| `parexec.py` | MEDIUM | Every parameter change. Performance-sensitive. |
| `externalizations.tsv` | NEVER EDIT | Managed exclusively by Embody. |

## Frozen Surfaces -- names that may never be renamed

These are load-bearing across a version boundary or across a process boundary,
so a rename does not break a build, it breaks something already shipped. The
promoted-surface census (`test_promoted_surface.py`) guards the third and
fourth; the rest are on you.

- **The cross-version update rendezvous.** An ALREADY-INSTALLED version composes
  `run()` strings naming the `updater` COMP, `UpdaterExt`, and
  `VerifyUpdate`/`VerifyRollback`/`StartupCheck` -- and executes them against
  the NEW tox. Rename one and every user updating *from* an older build lands
  on a missing name. `UpdaterExt` additionally gates a boot check on the
  `EmbodyExt` and `execute` DAT names existing, and **rolls a healthy update
  back** when they do not.
- **All MCP tool names and their parameter names.** They are persisted in
  `.embody/envoy-tools-cache.json` (served to already-running sessions before
  TD answers), relayed on the Convoy wire, and written into users'
  `settings.local.json` permission rules. A rename silently revokes a
  permission the user granted.
- **The documented `op.Embody` methods.** A user who ever read the docs -- or
  who has a generated rule file sitting in their own project -- still holds the
  old name. `test_promoted_surface.py::TestDocumentedApiIsPromoted` fails if one
  stops resolving.
- **Every custom parameter name.** There is no rename-migration map, and
  `_PERSISTED_PARAMS` keys `.embody/config.json` by name, so a renamed par
  silently loses the user's stored setting.
- **`Par.destroy()` is unrecoverable** -- it takes the value, expressions and
  exports. `_pruneRetiredPars` carries a proportional floor for exactly this
  reason; do not add a second par-removing path without one.
- **Two frozen cross-language contracts name a Python file as their mirror.**
  `platform/packages/contracts/envelope.ts` (C1, the `_embody_tdn` clipboard
  wire format) mirrors `dev/embody/Embody/TDXNExt.py`;
  `platform/packages/contracts/capability.ts` (C2, the TDXN capability scan
  output) mirrors `dev/embody/Embody/Collection/scanner.py`. Editing either
  Python side is a contract change: bump it and notify the dependents, which
  include the web submit gate and D1 `scans.capability_json`.
  **Corpus built 2026-08-29** (`platform/packages/scanner-ts/fixtures/`, mirrored to
  `dev/embody/unit_tests/fixtures/`): 21 fixtures across all eight surfaces plus
  evasion cases, run by `scanner-ts/src/parity.test.ts` and
  `dev/embody/unit_tests/test_scanner_parity.py`. First execution found TWO real
  divergences, both recorded in `SCANNER-SPEC.md` under "Known divergences" and
  pinned per-fixture. The load-bearing one is architectural: `scanner.py` parses
  Python and applies an allowlist; `scanner-ts` cannot parse Python and regex-matches
  a denylist, so `=op('x').destroy()` scores `clean` on the SERVER (the submit gate)
  and `flagged` in Embody. Do not "fix" it by appending identifiers -- that denylist
  is what the Python side deliberately abandoned. The ledger check fails both ways, so
  closing a gap requires deleting its `divergence` note in the same change.

## Embody's Own COMP Is Edited Live, Never Through Its `.tdn`

`_getTDNStrategyComps` omits Embody, its ancestors and its descendants, so
nothing reconstructs `/embody/Embody` from `dev/embody/Embody.tdn`. A hand edit
to that file (or to `tagger.tdn`, `toolbar.tdn`, `list.tdn`, `manager.tdn`) is
**inert** -- no reload reads it, the next save overwrites it from the live COMP,
and `git status` goes clean, so it looks applied and never was. Change the live
network; treat the `.tdn` as the receipt. Externalized `.py` DATs are the
opposite and are edited on disk as normal.

**Corollary: a descendant's own `.tdn` does NOT refresh on save.** The same
exclusion that makes a hand edit inert also means the pre-save export never
re-writes `tagger.tdn`, `toolbar.tdn`, `list.tdn` or `manager.tdn`. Only
`Embody.tdn` moves, and it carries a `tdn_ref` rather than their contents. So
after editing a live DAT inside one of them -- which is the sanctioned way to
change that code -- the receipt on disk goes **stale and stays stale**, and it
is committed. Re-export it explicitly:

    save_externalization(op_path='/embody/Embody/tagger')

Never `ExternalizeProject` for this (see `destructive-tests.md`). Verified
2026-08-29: three live parexec DATs renamed in WP4 wave 4d left `tagger.tdn`
holding the old method names through a full save, until the per-COMP
re-export.

## Project Save

- **`project.save()`** is the Python equivalent of Ctrl+S. It saves the .toe and automatically exports the release .tox to `release/`. No separate `ExportPortableTox` call is needed.
- **Save triggers the TDXN strip/restore cycle** - this blocks the main thread for 15+ seconds. The Envoy MCP operation timeout is 30s, so save may appear to time out but still completes. Use a long timeout or fire-and-forget.

## Sync Requirement
When updating a rule or skill in `.claude/`, also update the corresponding template DAT in `dev/embody/Embody/templates/` if one exists. The root CLAUDE.md and `text_claude.md` serve different audiences (Embody developers vs user projects) and are maintained independently. `text_help.py` covers UI-facing help only.
