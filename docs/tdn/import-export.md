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
| `embed_all` | `false` | Recurse into TDN-tagged COMPs instead of writing `tdn_ref` pointers, producing a self-contained export |

### What Gets Exported

- All operators under the root path (recursively)
- Only non-default parameter values
- Connections between operators
- Custom parameter definitions
- Built-in parameter sequences with non-default block counts or values (`sequences`)
- Flags, positions, sizes, colors, comments, tags
- Serializable operator storage entries (unless disabled per-COMP or globally)
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
| Pre | **Resolve templates and defaults** | Expand `$t` references and merge `type_defaults` into operators. With `clear_first`, excluded COMPs (the `tdn_exclude` tag) are preserved, not destroyed. |
| 1 | **Create operators** | Depth-first creation. COMPs first so children can be placed inside. |
| 2 | **Create custom parameters** | Pages, types, ranges, menu entries, defaults. |
| 2.5 | **Expand sequences** | Resizable parameter blocks (sequences on ops like `mathmixPOP`, `glslPOP`, `constantCHOP`) have their sequence parameters created before any values are set. |
| 3 | **Set parameter values** | Both built-in and custom. `=` prefix → expression, `~` prefix → bind. |
| 4 | **Set flags** | Array entries without `-` → `true`; with `-` → `false`. |
| 4a | **Warn about locked non-DATs** | Locked TOP/CHOP/SOP operators are flagged (lock preserved, frozen data is not). |
| 5 | **Wire connections** | Resolve sources (sibling name first, then full path). |
| 6 | **Set DAT content** | Text or table data loaded into DATs. |
| 6a | **Restore storage** | Storage key-value pairs restored via `op.store()`; `$type` wrappers deserialized. |
| 7 | **Set positions** | Positions, sizes, colors, comments applied (later phases still follow). |
| 7b | **Set docking** | Docking relationships restored between operators. |
| 7a | **Create annotations** | Annotations created with `utility=True`. |
| 8 | **Restore file links** | File/syncfile parameters restored on externalized DATs. |
| 8.5 | **Restore TOX content** | `.tox` content loaded into `tox_ref` shells. |
| 8.6 | **Restore nested TDN content** | `tdn_ref` shells filled from their own `.tdn` files in the same import (recursive, with an ancestor-chain cycle guard). Skipped by startup reconstruction and the post-save restore, whose own depth-sorted loops import every tracked TDN COMP exactly once. |
| 9 | **Apply target COMP properties** | The target COMP's own type, parameters, flags, color, tags applied last. |

### Version Compatibility

The importer checks metadata for compatibility:

- **`version`**: Warning **only when the file's version is newer** than the running build (the risky direction — the file may use schema this build does not understand). Equal or older versions import silently.
- **`td_build`**: Info message if TD version differs (parameter defaults may vary)
- **`build`**: Logged for informational purposes

These checks are non-blocking — import always proceeds.

---

## Diffing a Network

`diff_tdn` answers the question git cannot: **what have I changed but not saved?** It compares the **live in-memory network** against its on-disk `.tdn` — the unsaved window, which git never sees (git only reads files on disk, not TouchDesigner's live state).

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target` | (optional) | A COMP path, or a `.tdn` file path / bare filename (e.g. `"mixer.tdn"`). **Omit it for a whole-project summary** across every live TDN COMP. |
| `max_changed_ops` | `200` | Cap on the total changed entries returned (added + removed + modified combined, modified filled first). The reported `counts` always reflect the true totals; entries beyond the cap are dropped from the payload and counted in `dropped`. |
| `max_bytes` | `60000` | Cap on the output size. |

The comparison is **semantic, not byte-level**: both sides normalize through the same `type_defaults` / `par_templates` expansion, and the volatile export header (`build`, `generator`, `td_build`, `exported_at`, `source_file`) is ignored — so a no-op re-export shows nothing. Each change is `{old, new}` (old = disk, new = live), tagged `root`, `op`, or `annotation`.

### Git integration: the `.tdn` textconv driver

`diff_tdn` covers the *unsaved* window; for the *committed* view, Embody installs a git **textconv** driver so `git diff` / `git log -p` / `git show` on a `.tdn` show only real network changes, not export-header churn. It is auto-configured on Envoy startup (`.gitattributes` `*.tdn diff=tdn`, `.embody/tdn_textconv.py`, and `git config diff.tdn.textconv`). Use `diff_tdn` for what you have not saved; use `git diff` for what you have committed.

---

## Crash safety and `.tdn_backup`

Every `.tdn` write is **atomic** — the content goes to a temp file in the same directory, is flushed and `fsync`ed, then `os.replace()`s the target. A crash or power loss mid-write leaves either the complete old file or the complete new one, never a half-written one.

Before each write, Embody also rotates two generations of the previous content into a `.tdn_backup/` folder at your project root, mirroring the `.tdn`'s relative path:

```
my-project/
├── embody/
│   └── Foo/
│       └── bar.tdn          ← current
└── .tdn_backup/
    └── embody/
        └── Foo/
            ├── bar.tdn.bak   ← previous write
            └── bar.tdn.bak2  ← the one before that
```

After each write the file is read back and re-parsed; if that validation fails, the `.bak` is restored automatically. `ReconstructTDNComps` and the post-save export roll back from `.bak` the same way if reconstruction fails.

**`.tdn_backup/` is machine-local scratch and should not be committed.** It is superseded on every write and carries no history git does not already have — the `.tdn` files themselves are the versioned record. Envoy adds `.tdn_backup/` to your project's auto-managed [`.gitignore`](../embody/getting-started.md#auto-managed-gitignore) on startup. If your repo predates that entry and already tracks the folder, untrack it once:

```bash
git rm -r --cached .tdn_backup
```

Deleting `.tdn_backup/` at any time is safe — it is recreated on the next export.

---

## Error Handling

TDN import is **best-effort** — individual failures don't abort the entire operation.

| Situation | Behavior |
|-----------|----------|
| Unknown field | Ignored (forward compatibility) |
| Missing `name` or `type` | Skip operator silently (no log) |
| Missing connection source | Skip connection, log warning |
| Unrecognized parameter style | Skip parameter, log warning |
| Unrecognized flag | Ignored |
| Invalid parameter value | Attempt type coercion; skip with warning if impossible |
| Version newer than build | Log warning, proceed (older/equal versions import silently) |
| Unknown `$t` template reference | Log warning, skip page |

!!! info "Design principle"
    Log warnings for anything skipped so the developer can inspect the result. Never abort an entire import because a single operator, parameter, or connection failed — the partial result is more useful than no result.
