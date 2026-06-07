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

## Diffing a Network

There are two diff questions for a TDN-externalized COMP, and they need two different tools — because git can only see files on disk, never TouchDesigner's live in-memory network.

### What's UNSAVED — the `diff_tdn` MCP tool

`diff_tdn` compares the **live** network against its **on-disk `.tdn`** — i.e. what you've changed in TD but haven't saved yet. This is the view git fundamentally cannot provide.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target` | `""` | Empty (or `"/"`, `"project"`) → **whole project**: every live TDN COMP, summarized. Otherwise a COMP path **or** a `.tdn` file path/bare filename (e.g. `"tooltip.tdn"`), resolved to its COMP → that one COMP in full detail |
| `max_changed_ops` | `200` | Cap on reported changed operators (single-COMP); an honest `truncated` flag is set when exceeded. The project-wide call uses a fixed per-COMP cap of 50 |
| `max_bytes` | `60000` | Soft cap on envelope size; past it, per-field change bodies are dropped (`changed_keys` retained) |

The comparison is **semantic, not byte-level**: both sides are normalized through the same `type_defaults` / `par_templates` expansion the format uses (so compression-only differences read as no-change), and the volatile export header (`build`, `generator`, `td_build`, `exported_at`, `source_file`) is ignored. Operators are matched by name per level, so a reorder is clean and a deep child edit marks only that child — not its ancestors.

- **Single COMP** returns a diff envelope: `changed`, `counts{added,removed,modified}`, `added[]`, `removed[]`, `modified[{path,name,type,kind,changed_keys,changes}]`. Per-field changes carry **`old`=disk, `new`=live** — parameters as a list of `{name, old, new}`, other keys (flags, refs, root fields) as `{old, new}`. `kind` is `root | op | annotation`.
- **Project-wide** returns a summary: `{scope:'project', changed_count, clean_count, skipped_count, changed:[<envelope per changed COMP>], skipped, truncated}`. COMPs whose op no longer exists live are skipped, not exported.

Read-only, non-interactive, pull-only: it never prompts, never mutates TD, and is not auto-run. (Requires TD running.)

### What's COMMITTED — the `.tdn` git diff driver

For "what changed since my last commit / in history," use plain `git diff` / `git log -p` / `git show`. A raw git diff of a `.tdn` would be buried in export-header churn (a re-export bumps the timestamp/build even when nothing changed), so Embody installs a **git textconv diff driver** that strips that volatile header before diffing. The result: a re-export with no real change shows an **empty** diff, and only genuine network changes appear.

Embody auto-configures this the same way it manages `.gitignore`/`.gitattributes`/`.mcp.json` — on Envoy startup it:

1. ensures `.gitattributes` contains `*.tdn ... diff=tdn`,
2. deploys the converter script to `.embody/tdn_textconv.py` (pure stdlib — no TouchDesigner), and
3. registers it via `git config diff.tdn.textconv` (the driver definition must live in the repo's local git config — git refuses to run textconv commands defined by a cloned repo).

Nothing to install or run manually; `git diff` on a `.tdn` is clean from then on. The two paths are complementary: `diff_tdn` covers the unsaved window (git can't), and the driver keeps the committed/on-disk view (git's domain) just as clean.

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
