---
description: "Embody-specific conventions for extensions, logging, file safety, and MCP tool development"
paths:
  - "dev/embody/**"
  - "CLAUDE.md"
---
# Embody Code Conventions

## Extension Naming
Extension classes and source DATs must follow the `NameExt` convention (e.g., `EmbodyExt`, `EnvoyExt`, `TDNExt`, `TestRunnerExt`). Class name must match DAT name.

## Logging
Use `op.Embody.Log(message, level)` from anywhere. Levels: `'DEBUG'`, `'INFO'`, `'WARNING'`, `'ERROR'`, `'SUCCESS'`. Convenience methods: `op.Embody.Debug(msg)`, `.Info()`, `.Warn()`, `.Error()`. Logs go to FIFO DAT, textport, log file (`dev/logs/`), and ring buffer.

## File Safety
- Always use forward slashes (`/`) in file paths
- Only delete files tracked by Embody: `isTrackedFile()`, `safeDeleteFile()`
- Directory cleanup: `rmdir()` only (fails on non-empty) — never `shutil.rmtree()`
- `externalizations.tsv` is managed exclusively by Embody — NEVER edit directly

## Parameter Handling
- No `hasattr` for known parameters — Embody's custom pars are static and locked in the `.toe`
- Use them directly: `self.ownerComp.par.Envoystatus = 'Running'`

## MCP Tool Development
- **Error types**: (1) Protocol errors (FastMCP handles automatically), (2) Tool execution errors via `{'error': str(e)}` dicts
- **Input validation**: Validate all inputs before passing to TD. Check paths, verify operators exist, sanitize strings for `eval()`/`exec()`
- **Tool signatures are API contracts**: Changing parameter names, type hints, or docstrings in `_register_tools()` changes the public MCP interface
- **Localhost only**: `127.0.0.1`, never `0.0.0.0`

## Operator Management
- **Renaming**: Only rename the operator itself (via MCP `rename_op` or inside TD). NEVER rename externalized files on disk, NEVER manually update `file`/`externaltox`, NEVER edit the table. `checkOpsForContinuity` handles everything.
- **Creating Python files**: Always create the textDAT in TD first, then externalize via Embody. Never manually set `file`/`syncfile` parameters.
- **Never cache extension references** (HARD RULE): Never assign a TD extension to a variable. Always reference it inline at the point of use.
  - WRONG: `emb = op.Embody.ext.Embody` then `emb.Foo()`; `self.envoy = self.embody.ext.Envoy` then `self.envoy.Bar()`; `strip = self.ownerComp.ext.TDN._stripBuildSuffix` then `strip(x)` (a bound method holds the instance too).
  - RIGHT: `op.Embody.ext.Embody.Foo()`, `self.ownerComp.ext.TDN.Bar()`, `self.embody.ext.Envoy.Baz()` -- resolve the chain every time.
  - WHY: TD reinitializes an extension whenever its externalized `.py` changes on disk (and on reload); any cached reference then points at a dead instance and silently misbehaves.
  - NOT this rule (fine to assign): the return *value* of a call (`result = op.Embody.ext.TDN.ExportNetwork(...)`), a DAT/op the extension exposes (`table = op.Embody.ext.Embody.Externalizations`), or the COMP itself (`self.embody`, `self.my`, `self.ownerComp`). Only the *extension object* (and its bound methods) may not be stored.
  - EXCEPTIONS -- only when holding the reference is genuinely unavoidable, mark the assignment with a trailing `# ext-cache-ok` comment so the guard test (`test_no_ext_caching.py`) ignores it: (1) main-thread -> worker handoff (resolve `op...ext...` on the main thread and pass it into a background thread; resolving on a worker is a thread conflict); (2) test monkeypatch save/restore (capture an extension's original method so a `finally` block can restore it after patching). Convenience is never an exception.
  - ENFORCED: `dev/embody/unit_tests/test_no_ext_caching.py` scans production and test source and fails on any new occurrence.

## File Editing Impact

| File | Impact | Notes |
|------|--------|-------|
| `EmbodyExt.py` | HIGH | Core engine. All externalization behavior. |
| `EnvoyExt.py` | HIGH | MCP server. Tool signature changes break API. |
| `TDNExt.py` | MEDIUM | `.tdn` format compatibility. |
| `execute.py` | LOW | Lifecycle callbacks. Rarely changes. |
| `parexec.py` | MEDIUM | Every parameter change. Performance-sensitive. |
| `externalizations.tsv` | NEVER EDIT | Managed exclusively by Embody. |

## Project Save

- **`project.save()`** is the Python equivalent of Ctrl+S. It saves the .toe and automatically exports the release .tox to `release/`. No separate `ExportPortableTox` call is needed.
- **Save triggers the TDN strip/restore cycle** — this blocks the main thread for 15+ seconds. The Envoy MCP operation timeout is 30s, so save may appear to time out but still completes. Use a long timeout or fire-and-forget.

## Sync Requirement
When updating a rule or skill in `.claude/`, also update the corresponding template DAT in `dev/embody/Embody/templates/` if one exists. The root CLAUDE.md and `text_claude.md` serve different audiences (Embody developers vs user projects) and are maintained independently. `text_help.py` covers UI-facing help only.
