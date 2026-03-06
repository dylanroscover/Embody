---
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
- **Never cache extension references**: Always call inline: `self.ownerComp.ext.Embody.Method()`. Cached refs go stale on reinit.

## File Editing Impact

| File | Impact | Notes |
|------|--------|-------|
| `EmbodyExt.py` | HIGH | Core engine. All externalization behavior. |
| `EnvoyExt.py` | HIGH | MCP server. Tool signature changes break API. |
| `TDNExt.py` | MEDIUM | `.tdn` format compatibility. |
| `execute.py` | LOW | Lifecycle callbacks. Rarely changes. |
| `parexec.py` | MEDIUM | Every parameter change. Performance-sensitive. |
| `externalizations.tsv` | NEVER EDIT | Managed exclusively by Embody. |

## Sync Requirement
When updating a rule or skill in `.claude/`, also update the corresponding template DAT in `dev/embody/Embody/templates/` if one exists. The root CLAUDE.md and `text_claude.md` serve different audiences (Embody developers vs user projects) and are maintained independently. `text_help.py` covers UI-facing help only.
