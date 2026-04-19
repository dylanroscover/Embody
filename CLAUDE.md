# Embody + Envoy

## Project Overview

**Embody** is a TouchDesigner extension that automates externalization of COMP and DAT operators to version-control-friendly files (.tox, .py, .json, .xml, etc.). It solves the problem of TouchDesigner's binary .toe files being impossible to diff/merge in git.

**Envoy** is an MCP (Model Context Protocol) server embedded inside Embody that lets Claude Code create, modify, connect, and query TouchDesigner operators programmatically — plus manage Embody externalizations.

## Critical Rules

1. **Prefer `.tdn` files for reading TDN-externalized COMPs** — `.tdn` files are JSON on disk with complete network structure (operators, parameters, connections, positions, flags, DAT content, annotations). Reading them directly is faster than MCP round-trips. Check `externalizations.tsv` (strategy column) or call `get_externalizations` to identify TDN-strategy COMPs. To edit: modify the `.tdn` file on disk, then **always** call `import_network` via MCP with the COMP path, the parsed JSON, and `clear_first=True` to reload it in TD. **Never leave a `.tdn` edit unreloaded** — the user must see updates immediately in TD. Use MCP when you need live runtime state (evaluated expressions, cook errors) or for non-TDN operators.
2. **Use Envoy MCP tools for live TD state and non-TDN operators** — NEVER say "I can't edit that because it's in a .tox" or "these are binary files I can't access." For operators not externalized as TDN, use MCP tools to inspect and modify them. The filesystem holds externalized files (`.py`, `.tox`, `.tdn`, `.json`, `.xml`, etc.); MCP is for interacting with live operator state inside TD.
3. **NEVER create operators under `/local`** — `/local` is volatile storage, not saved with the `.toe` file. Always place operators under the project root or the user's active network. Use `execute_python` with `result = ui.panes.current.owner.path` to find the current network.
4. **Do NOT assume network paths** — never guess `/project1`. Use `query_network` on `/` to discover the actual root structure.
5. **Default to the current network** — use `execute_python` with `result = ui.panes.current.owner.path` to find the active pane.
6. **Always consult the TD wiki** before writing TD Python code OR claiming TD behavior — confirm API behavior, file formats, and application features against official Derivative documentation even if you're confident. Never assume a TD feature, file type, or convention exists without a verified source.
7. **Binary files** (`.toe`, `.tox`) — use MCP tools to inspect contents, not the filesystem.
8. **Always check for errors after creating operators** — `get_op_errors` with `recurse=true` immediately after creating and connecting operators.
9. **Favor annotations over OP comments** — use `create_annotation` for documenting operators and groups.
10. **Always analyze log files after MCP operations** — read `dev/logs/` for the complete picture. Ring buffer only holds 200 entries.
11. **Always update unit tests when modifying project code** — check whether existing tests assert against changed behavior.
12. **Batch repetitive MCP operations** — never make 3+ individual calls to the same tool. Use `batch_operations` to combine `set_op_position`, `connect_ops`, `set_parameter`, `set_op_flags`, etc. into a single request. For complex logic (conditionals, loops, computed values), use `execute_python` instead. Each MCP round-trip costs tokens and latency — minimize them.

## Approach Guidelines

- Before editing a file, verify it is the ACTUAL file responsible. Grep and trace the render path before making changes.
- Avoid over-engineering. Prefer minimal, targeted changes.
- When debugging, state your hypothesis, verify with evidence, then fix.

## Project Structure

```
Embody/
├── CLAUDE.md                              # This file — slim north star
├── .claude/
│   ├── commands/                         # User-invocable slash commands
│   │   ├── run-tests.md                 # /run-tests — run test suite via MCP
│   │   ├── status.md                    # /status — project health check
│   │   └── explore-network.md           # /explore-network — discover TD network
│   ├── rules/                            # Always-loaded conventions
│   │   ├── network-layout.md            # Grid, spacing, annotation coords
│   │   ├── td-python.md                 # TD Python gotchas and rules
│   │   ├── mcp-safety.md               # Thread boundary, localhost, timeouts
│   │   ├── skill-prerequisites.md       # Which skills to load before MCP calls
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
│       ├── mcp-tools-reference/         # Complete MCP tool catalog
│       └── multi-instance/              # Multi-instance bridge workflow
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
│   ├── Embody-5.toe                      # Active development project
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

Dual-thread design: worker thread runs MCP server (no TD imports), main thread executes TD operations via `_onRefresh()`. Communication via `threading.Event` + `Queue`. Server auto-configures `.mcp.json` in the git root (or project folder if no git) on startup.

### TDN Network Format

JSON-based format for representing TD networks as diffable text. Non-default parameters only, expression shorthand (`=` prefix), type defaults, parameter templates. Full spec: `docs/tdn/specification.md`

## Extension Referencing

```python
# Promoted methods (uppercase) — called directly on the component:
op.Embody.Update()
op.Embody.Save()
op.Embody.InitEnvoy()    # Regenerate MCP + AI client config files
op.Embody.InitGit()      # Init/reconnect git repo + .gitignore/.gitattributes
op.Embody.ExportPortableTox(target=some_comp, save_path='/path/to/output.tox')

# Non-promoted (lowercase) — through ext:
op.Embody.ext.Embody.getExternalizedOps()
op.Embody.ext.Envoy.Start()
```

**NEVER cache extension references in variables** — always call inline.

## Key References

- **TD Wiki**: https://docs.derivative.ca/Main_Page
- **TD Python API**: MUST load `/td-api-reference` before writing TD Python code
- **MCP Tools**: MUST load `/mcp-tools-reference` before first MCP tool call in session
- **Tests**: Use the `/run-tests` skill for running and writing tests
- **TDN Spec**: See `docs/tdn/specification.md` for the full format specification
