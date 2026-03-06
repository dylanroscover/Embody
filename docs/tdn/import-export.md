# Import & Export

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
| `tdn` | The `.tdn` JSON document (full document or operators array) |
| `clear_first` | Delete existing children before importing |

### Import Phases

The import process runs in a pre-phase plus seven sequential phases. This ordering ensures dependencies are satisfied:

| Phase | Action | Details |
|-------|--------|---------|
| Pre | **Resolve templates and defaults** | Expand `$t` references and merge `type_defaults` into operators |
| 1 | **Create operators** | Depth-first creation. COMPs first so children can be placed inside. |
| 2 | **Create custom parameters** | Pages, types, ranges, menu entries, defaults. |
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
