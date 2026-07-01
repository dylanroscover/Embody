# Read, Import & Export

## Reading a Network (no disk I/O)

### MCP Tool

Use the `read_tdn` tool to return the live network as a TDN dict **without writing anything to disk**. This is the preferred read path for LLM workflows exploring networks of more than ~3 operators — **typically 20-90× fewer tokens** than walking the same subtree with `get_op` + `query_network` because of default-omission, `type_defaults`, and `par_templates` compaction.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `comp_path` | `"/"` | Starting COMP path |
| `include_dat_content` | Toggle setting | Include DAT text/table content |
| `max_depth` | `null` (unlimited) | Cap recursion on large roots |
| `embed_all` | `false` | Recurse into TDN-tagged COMPs instead of skipping their children |

Works in all three `Tdnmode` values (Off / Export-on-Save / Roundtrip) — `read_tdn` reads live state, not `.tdn` files on disk.

### When NOT to use `read_tdn`

For these, reach for the runtime-state MCP tools instead:

| Need | Use |
|---|---|
| Evaluated-expression runtime values | `get_parameter` |
| Cook errors / warnings | `get_op_errors` |
| DAT / CHOP / TOP output data | `get_dat_content`, `capture_top` |
| Cook timing | `get_op_performance` |
| Flag state after runtime mutation | `get_op_flags` |

---

## Exporting a Network

### Keyboard Shortcuts

- ++ctrl+shift+e++ — Export the entire project to a single `.tdn` file
- ++ctrl+alt+e++ — Export just the current COMP to a `.tdn` file

### MCP Tool

Use the `export_network` tool with these options:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `root_path` | `"/"` | Starting COMP path |
| `include_dat_content` | Toggle setting | Include DAT text/table content |
| `output_file` | `null` | File path (use `"auto"` for automatic naming, `null` for dict-only) |
| `max_depth` | `null` (unlimited) | Maximum recursion depth |

### What Gets Exported

- All operators under the root path (recursively)
- Only non-default parameter values
- Connections between operators
- Custom parameter definitions
- Flags, positions, sizes, colors, comments, tags
- Annotations
- Optionally: DAT text/table content

### What Gets Excluded

- System paths (`/local`, `/sys`, `/perform`, `/ui`)
- Pulse, Momentary, and Header parameters (no persistent state)
- Read-only parameters
- COMP externalization parameters (`externaltox`, `enableexternaltox`, etc.)
- Children of palette clones (TD recreates them from the clone source)

---

## Importing a Network

Use the `import_network` MCP tool:

| Parameter | Description |
|-----------|-------------|
| `target_path` | Destination COMP path |
| `tdn` | The TDN document, parsed object (full document or operators array) |
| `clear_first` | Delete existing children before importing |

### Import Phases

The import process runs in a pre-phase plus the ordered phases below. This ordering ensures dependencies are satisfied:

| Phase | Action | Details |
|-------|--------|---------|
| Pre | **Resolve templates and defaults** | Expand `$t` references and merge `type_defaults` into operators |
| 1 | **Create operators** | Depth-first creation. COMPs first so children can be placed inside. |
| 2 | **Create custom parameters** | Pages, types, ranges, menu entries, defaults. |
| 2.5 | **Expand sequences** | Resizable parameter blocks (sequences on ops like `mathmixPOP`, `glslPOP`, `constantCHOP`) have their sequence parameters created before any values are set. |
| 3 | **Set parameter values** | Both built-in and custom. `=` prefix → expression, `~` prefix → bind. |
| 4 | **Set flags** | Array entries without `-` → `true`; with `-` → `false`. |
| 5 | **Wire connections** | Resolve sources (sibling name first, then full path). |
| 6 | **Set DAT content** | Text or table data loaded into DATs. |
| 7 | **Set positions** | Positions, sizes, colors, comments applied last. |
| 7a | **Create annotations** | Annotations created with `utility=True`. |

### Version Compatibility

The importer checks metadata for compatibility:

- **`version`**: Warning if format version differs
- **`td_build`**: Info message if TD version differs (parameter defaults may vary)
- **`build`**: Logged for informational purposes

These checks are non-blocking — import always proceeds.

---

## Diffing a Network

`diff_tdn` answers the question git cannot: **what have I changed but not saved?** It compares the **live in-memory network** against its on-disk `.tdn` — the unsaved window, which git never sees (git only reads files on disk, not TouchDesigner's live state).

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target` | (optional) | A COMP path, or a `.tdn` file path / bare filename (e.g. `"mixer.tdn"`). **Omit it for a whole-project summary** across every live TDN COMP. |
| `max_changed_ops` | `200` | Cap on the number of changed operators reported. |
| `max_bytes` | `60000` | Cap on the output size. |

The comparison is **semantic, not byte-level**: both sides normalize through the same `type_defaults` / `par_templates` expansion, and the volatile export header (`build`, `generator`, `td_build`, `exported_at`, `source_file`) is ignored — so a no-op re-export shows nothing. Each change is `{old, new}` (old = disk, new = live), tagged `root`, `op`, or `annotation`.

### Git integration: the `.tdn` textconv driver

`diff_tdn` covers the *unsaved* window; for the *committed* view, Embody installs a git **textconv** driver so `git diff` / `git log -p` / `git show` on a `.tdn` show only real network changes, not export-header churn. It is auto-configured on Envoy startup (`.gitattributes` `*.tdn diff=tdn`, `.embody/tdn_textconv.py`, and `git config diff.tdn.textconv`). Use `diff_tdn` for what you have not saved; use `git diff` for what you have committed.

---

## Error Handling

TDN import is **best-effort** — individual failures don't abort the entire operation.

| Situation | Behavior |
|-----------|----------|
| Unknown field | Ignored (forward compatibility) |
| Missing `name` or `type` | Skip operator, log error |
| Missing connection source | Skip connection, log warning |
| Unrecognized parameter style | Skip parameter, log warning |
| Unrecognized flag | Ignored |
| Invalid parameter value | Attempt type coercion; skip with warning if impossible |
| Version mismatch | Log warning, proceed |
| Unknown `$t` template reference | Log warning, skip page |

!!! info "Design principle"
    Log warnings for anything skipped so the developer can inspect the result. Never abort an entire import because a single operator, parameter, or connection failed — the partial result is more useful than no result.
