# Embody + Envoy

## Project Overview

**Embody** is a TouchDesigner extension that automates externalization of COMP and DAT operators to version-control-friendly files (.tox, .py, .json, .xml, etc.). It solves the problem of TouchDesigner's binary .toe files being impossible to diff/merge in git.

**Envoy** is an MCP (Model Context Protocol) server embedded inside Embody that lets Claude Code create, modify, connect, and query TouchDesigner operators programmatically — plus manage Embody externalizations.

## Critical Rules

1. **ALWAYS use Envoy MCP tools to inspect and modify anything inside TouchDesigner** — NEVER say "I can't edit that because it's in a .tox" or "these are binary files I can't access." Use MCP tools for everything in the live TD environment. The filesystem holds externalized files (`.py`, `.tox`, `.tdn`, `.json`, `.xml`, etc.); MCP is for interacting with operators, parameters, and network state inside TD.
2. **Do NOT assume network paths** — never guess `/project1`. Use `query_network` on `/` to discover the actual root structure.
3. **Default to the current network** — use `execute_python` with `result = ui.panes.current.owner.path` to find the active pane.
4. **Never edit `externalizations.tsv` directly** — managed exclusively by Embody's tracking system.
5. **Always use forward slashes** in file paths for cross-platform compatibility.
6. **Always consult the TD wiki** before writing TD Python code — confirm API behavior even if you're confident.
7. **Binary files** (`.toe`, `.tox`) — use MCP tools to inspect contents, not the filesystem.
8. **Thread boundary**: `EnvoyMCPServer` (worker thread) must never import TD modules. All TD access goes through `_execute_in_td()` → main thread.
9. **Safe deletion only**: Use `safeDeleteFile()` / `isTrackedFile()`. Never delete untracked files.
10. **Always check for errors after creating operators** — `get_op_errors` with `recurse=true` immediately after creating and connecting operators.
11. **When updating a rule or skill** in `.claude/`, also update the corresponding template DAT in `dev/embody/Embody/templates/` if one exists. Root CLAUDE.md and `text_claude.md` serve different audiences and are maintained independently. `text_help.py` covers UI-facing help only.
12. **Favor annotations over OP comments** — use `create_annotation` for documenting operators and groups.
13. **Always analyze log files after MCP operations** — read `dev/logs/` for the complete picture. Ring buffer only holds 200 entries.
14. **Always update unit tests when modifying project code** — check whether existing tests assert against changed behavior.

## Approach Guidelines

- Before editing a file, verify it is the ACTUAL file responsible. Grep and trace the render path before making changes.
- Avoid over-engineering. Prefer minimal, targeted changes.
- When debugging, state your hypothesis, verify with evidence, then fix.

## Project Structure

```
Embody/
├── CLAUDE.md                              # This file — slim north star
├── .claude/
│   ├── rules/                            # Always-loaded conventions
│   │   ├── network-layout.md            # Grid, spacing, annotation coords
│   │   ├── td-python.md                 # TD Python gotchas and rules
│   │   ├── mcp-safety.md               # Thread boundary, localhost, timeouts
│   │   └── embody-code-conventions.md   # Path-scoped to dev/embody/**
│   └── skills/                           # On-demand workflows and reference
│       ├── create-operator/             # Operator creation workflow
│       ├── debug-operator/              # Error diagnosis workflow
│       ├── externalize-operator/        # Externalization workflow
│       ├── create-extension/            # Extension creation guide
│       ├── manage-annotations/          # Annotation coordinate math
│       ├── add-mcp-tool/               # Adding MCP tools (dev only)
│       ├── run-tests/                   # Test suite runner (dev only)
│       ├── td-api-reference/            # Full TD Python API reference
│       └── mcp-tools-reference/         # Complete MCP tool catalog
├── docs/                                  # MkDocs documentation site
│   ├── embody/                           # Embody feature docs
│   ├── envoy/                            # Envoy MCP server docs
│   ├── tdn/                              # TDN format docs
│   │   └── specification.md             # TDN format specification
│   ├── td-development/                   # TD coding best practices
│   ├── tdn.schema.json                   # JSON Schema for .tdn validation
│   ├── testing.md                        # Test framework docs
│   └── changelog.md                      # Version history
├── dev/
│   ├── Embody-5.140.toe                  # Active development project
│   ├── .venv/                            # Python virtual environment (auto-created)
│   ├── Backup/                           # Versioned .toe backups
│   └── embody/
│       ├── externalizations.tsv          # Tracking table (managed by Embody)
│       └── Embody/                       # Main extension source
│           ├── EmbodyExt.py              # Core externalization engine
│           ├── EnvoyExt.py               # MCP server extension
│           ├── TDNExt.py                 # TDN network format export/import
│           ├── text_claude.md            # Template for user-project CLAUDE.md
│           ├── execute.py                # Project lifecycle callbacks
│           ├── parexec.py                # Parameter change callbacks
│           └── templates/                # Templates for generated rules/skills
└── release/
    └── Embody-v*.tox                     # Latest release build
```

## Architecture

### Externalization Sync (.toe <-> externalized files)

Embody externalizes tagged operators to files under `dev/embody/` — `.py` for DATs, `.tox` for COMPs (TOX strategy), `.tdn` for COMPs (TDN strategy). Edits to externalized files are read by TD on load/sync; changes inside TD are written out on save. Externalized files on disk are the source of truth.

### Automatic Restoration

On project open, Embody runs a three-phase startup:
- **Frame 30**: `_upgradeEnvoy()` — extract Claude config if Envoy enabled but missing
- **Frame 45**: `RestoreTOXComps()` — restore TOX-strategy COMPs from `.tox` files
- **Frame 60**: `ReconstructTDNComps()` — rebuild TDN-strategy COMPs from `.tdn` files

All externalized operators are fully recoverable from disk, regardless of `.toe` save state.

### Envoy MCP Architecture

Dual-thread design: worker thread runs MCP server (no TD imports), main thread executes TD operations via `_onRefresh()`. Communication via `threading.Event` + `Queue`. Server auto-configures `.mcp.json` in the git root on startup.

### TDN Network Format

JSON-based format for representing TD networks as diffable text. Non-default parameters only, expression shorthand (`=` prefix), type defaults, parameter templates. Full spec: `docs/tdn/specification.md`

## Extension Referencing

```python
# Promoted methods (uppercase) — called directly on the component:
op.Embody.Update()
op.Embody.Save()
op.Embody.ExportPortableTox(target=some_comp, save_path='/path/to/output.tox')

# Non-promoted (lowercase) — through ext:
op.Embody.ext.Embody.getExternalizedOps()
op.Embody.ext.Envoy.Start()
```

**NEVER cache extension references in variables** — always call inline.

## Developing Embody

### File Editing Impact

| File | Impact | Notes |
|------|--------|-------|
| `EmbodyExt.py` | HIGH | Core engine. All externalization behavior. |
| `EnvoyExt.py` | HIGH | MCP server. Tool signature changes break API. |
| `TDNExt.py` | MEDIUM | `.tdn` format compatibility. |
| `parexec.py` | MEDIUM | Every parameter change. Performance-sensitive. |
| `externalizations.tsv` | NEVER EDIT | Managed exclusively by Embody. |

### Key References

- **TD Wiki**: https://docs.derivative.ca/Main_Page
- **TD Python API**: Use the `/td-api-reference` skill for full reference
- **MCP Tools**: Use the `/mcp-tools-reference` skill for the complete tool catalog
- **Tests**: Use the `/run-tests` skill for running and writing tests
- **TDN Spec**: See `docs/tdn/specification.md` for the full format specification
