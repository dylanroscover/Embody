# Externalization Details

## Build Tracking

Embody adds and updates an **About** page on every externalized COMP with:

- **Build Number** — incremented each time the COMP is saved
- **Touch Build** — the TouchDesigner version used for the save
- **Build Date** — UTC timestamp of when the `.tox` was written

This provides robust version tracking directly on your components.

## Folder Configuration

The externalization folder can be configured in several ways:

- **Static Path**: Set a folder name like `externals` to save to `{project.folder}/externals/`
- **Expression Mode**: Use Python expressions for dynamic paths (e.g., `project.folder + '/build_' + str(app.build)`)
- **Existing Folders**: You can point Embody at a folder containing other files — Embody will only manage its own tracked files and leave others untouched.

!!! note
    When changing the folder location, Embody will migrate tracked files to the new location and clean up empty directories in the old location.

## Duplicate Path Handling

When Embody detects multiple operators pointing to the same external file, it groups them and resolves the duplicates:

**Automatic resolution (COMPs):** If the operators are COMPs with TouchDesigner clone relationships (`enablecloning` / `clone` parameter), Embody automatically identifies the clone master and tags the others with a `clone` tag — no dialog needed.

**Manual resolution:** For DATs or COMPs without TD clone relationships, Embody shows a single dialog listing all operators that share the path. You select which operator is the **master**; the others receive a `clone` tag.

- Selecting a master tags all other operators as clones. Changes to the shared file affect all of them.
- **Dismiss** skips the group for now. Embody will re-prompt on the next Update cycle.

Once any operator in a group has a `clone` tag, the entire group is considered resolved and Embody will not prompt again.

Enable or disable this check with the `Detect Duplicate Paths` parameter.

## Externalizations Table

Embody maintains an `externalizations` tableDAT outside the Embody component with the following columns:

| Column | Description |
|--------|-------------|
| `path` | TouchDesigner operator path (e.g., `/project/base1`) |
| `type` | Operator type (e.g., `base`, `text`, `table`) |
| `rel_file_path` | Relative file path from project folder |
| `timestamp` | Last save time in UTC |
| `dirty` | Dirty state (`True`, `False`, or `Par` for parameter changes) |
| `build` | Build number (COMPs only) |
| `touch_build` | TouchDesigner build version (COMPs only) |
| `strategy` | Externalization strategy (`tox`, `tdn`, `py`, `txt`, etc.) |
| `node_x` | Operator X position in the network (for restoration) |
| `node_y` | Operator Y position in the network (for restoration) |
| `node_color` | Operator node color (for restoration) |

This table serves as the source of truth for what files Embody manages. Only files listed here will ever be deleted by Embody.

!!! warning
    Never edit the `externalizations.tsv` file directly. It is managed exclusively by Embody's tracking system.

## TDN Strategy

COMPs can also be externalized using the **TDN strategy** instead of `.tox`. This exports the COMP's network as human-readable JSON (`.tdn` files) instead of binary `.tox` files, enabling meaningful git diffs, code review, three-way merges, and schema-validated CI.

See [TDN Format](../tdn/index.md) for format details, and ["Why TDN"](#why-tdn) below for the concrete wins.

### TDN Mode (master switch)

The `Tdnmode` parameter on the Embody COMP selects how the TDN subsystem behaves at save/open time:

| Mode | On save (++ctrl+s++) | On project open | When to pick |
|------|----------------------|-----------------|--------------|
| **Off** | No TDN activity. `.tdn` files on disk stay untouched. | No reconstruction. | Temporarily disabling TDN without deleting any files. |
| **Export-on-Save (MCP)** *(default, recommended)* | Writes `.tdn` files for every tagged TDN COMP **whose content changed** since the last save (unchanged COMPs are skipped to avoid noisy git diffs from header churn). `.toe` stays the source of truth; live network is never stripped. | No reconstruction — the `.toe` already has everything. | Day-to-day work. Cheap, predictable, no round-trip risk. Ideal for git-diff / MCP workflows. |
| **Full Import/Export (Experimental)** | Writes `.tdn` files **and** strips COMP children from the `.toe` so the `.toe` stays small. | Children are rebuilt from `.tdn` files at frame 60. | Large projects where the `.toe` bloats without strip, or workflows that treat `.tdn` as the primary source. May hit edge cases with palette clones and extension reload timing. |

You can switch modes at any time — existing `.tdn` files on disk and tracked COMP entries are preserved across transitions.

!!! note "Opt-in per COMP"
    Regardless of mode, only COMPs you've explicitly tagged with Embody's TDN tag are touched. A fresh `baseCOMP` you just created is invisible to Embody until you tag it.

### Content Safety (save-time check)

When you save a project (++ctrl+s++), Embody checks for **unprotected content** inside TDN-managed COMPs:

- **At-risk DATs** — DATs that contain content but are neither externalized (no Embody tag) nor embedded (the **Embed DATs in TDNs** parameter is OFF).
- **At-risk storage** — `comp.storage` entries on the TDN COMP or its descendants that won't be preserved when **Embed Storage in TDNs** is OFF.

If at-risk content is found, Embody prompts you with three options:

| Button | Behavior |
|--------|----------|
| **Externalize DATs** | Tag and externalize the at-risk DATs so their content is saved to files on disk. Storage has no externalization path — enable **Embed Storage in TDNs** to preserve it. |
| **Skip** | Proceed with the save. Skipped content is logged at SUCCESS level so you know exactly what was dropped. |
| **Always Externalize** | Externalize now, and do so automatically on future saves without asking. |

The preference is stored in the **Content Safety** parameter (`Tdndatsafety`) and can be changed at any time from the Embody COMP's TDN settings. Setting `Tdndatsafety = 'ignore'` explicitly suppresses the check entirely — an opt-in escape hatch for power users who accept the risk.

!!! tip
    To avoid this prompt entirely, either enable **Embed DATs in TDNs** / **Embed Storage in TDNs** (stores content directly in the `.tdn` file) or externalize your DATs with Embody tags before saving.

!!! warning "Locked TOPs, CHOPs, and SOPs lose their frozen data"
    TDN cannot store frozen pixel, channel, or geometry data. If your network contains locked non-DAT operators, their lock flag is preserved but their content will be **empty after reload** when using Full mode. Use **TOX strategy** instead of TDN for COMPs that contain locked TOPs, CHOPs, or SOPs. See [Lock Flag Limitation](../tdn/specification.md#lock-flag-limitation) for details.

### Why TDN

TDN isn't just a different file format — it unlocks workflows that binary `.toe`/`.tox` files can't support.

**File size and density.** Even without compression, `.tdn` is comparable to or smaller than the equivalent binary `.tox` because only non-default parameters are emitted. Three compaction mechanisms kick in:

- Default omission — parameters are included only when they differ from the operator type's creation defaults.
- `type_defaults` — properties shared across every operator of a type are hoisted once to a top-level block and stripped from each operator.
- `par_templates` — repeated custom-parameter pages collapse into references.

A real leaf-component file like `envoy_toggle.tdn` is ~1.3 KB — 38 readable lines including only the ~15 parameters whose values actually differ from a `textCOMP`'s defaults.

**Git three-way merge on real conflicts.** `.toe` is binary, so git can't three-way merge it — one side wins, the other loses. `.tdn` is JSON; git merges it like any other text file, and conflicts show up as readable diffs you can resolve by reading intent:

```
"Speed": {
<<<<<<< HEAD
    "value": 1.5
=======
    "value": 2.0
>>>>>>> feature/faster-playback
}
```

**PR review humans can actually do.** A `.toe` diff is literally `Binary files differ`. A `.tdn` parameter change is a one-line delta. Reviewers comment on specific lines, request changes, and approve — the same workflow as any other text code review.

**Cross-version portability.** `.toe` and `.tox` are coupled to the exact TD build that wrote them. `.tdn` files are format-versioned and self-describing — every export stamps its own `version`, `td_build`, and `generator`. As long as the referenced operator types exist in the current TD build, the network rebuilds cleanly.

**CI/CD integration.** The `docs/tdn.schema.json` JSON Schema (draft 2020-12) validates every `.tdn` file in CI. You can compute diff stats (operators added/removed, parameters changed), lint for forbidden patterns (absolute paths, missing help text, orphan ops), and gate merges — none of which is possible with binary `.toe`.

**Dramatically lower token cost for LLM / MCP workflows.** Reading a network via `read_tdn` (MCP tool) uses **~20-90× fewer tokens** than walking the same subtree via `get_op`+`query_network`:

- `get_op` returns all 175-219 parameters per operator wrapped in `{value, mode, label}` triples — roughly 15-25 KB per operator.
- `read_tdn` applies the same compaction as `.tdn` export — default omission, `type_defaults`, `par_templates` — and returns the full subtree in one call.

For a 24-operator COMP (`container_left.tdn`), the TDN payload is ~12 KB (~3K tokens) vs an estimated ~360-480 KB (~90-120K tokens) via an equivalent `get_op` walk. The delta scales with network size and type homogeneity. A conservative 5× floor is verified in CI (`test_mcp_tdn_tools.py`); 20-90× is the typical real-world range. See the [Claude Code skills guide](../envoy/claude-code.md) for which Envoy skill to consult and when to prefer `read_tdn` vs the runtime probes (`get_parameter`, `get_op_errors`, `get_dat_content`, etc.).

## Automatic Restoration

Embody automatically restores all externalized operators when a project is opened. Your externalized files on disk are the source of truth — you do not need to save your `.toe` file to preserve externalized work.

| Strategy | Restoration Method | Toggle |
|----------|-------------------|--------|
| **TOX** | Missing COMPs are restored from `.tox` files on disk | `Toxrestoreonstart` (ON by default) |
| **TDN** | Children are reconstructed from `.tdn` JSON files — **Full mode only** | `Tdnmode = Full` + `Tdncreateonstart` |
| **DAT** | Synced from external files via TouchDesigner's native `file` parameter | Always active |

In **Full** mode the `.toe` is kept small (children are stripped on save) and rebuilt from `.tdn` on open, so the files on disk are the source of truth. In **Export-on-Save** mode the `.toe` keeps a complete copy of every COMP, so there's nothing to reconstruct — the `.toe` is the source of truth, and `.tdn` files exist purely for git diff / MCP reads.

## Export Portable Tox

Export any COMP as a **self-contained `.tox`** with all external file references and Embody tags stripped. The exported `.tox` works when loaded into any TouchDesigner project — no missing file errors and no Embody metadata.

### How it works

`ExportPortableTox()` temporarily strips all relative `file`/`syncfile` references from DATs, `externaltox`/`enableexternaltox` references from COMPs, and all Embody tags from every operator, saves the `.tox`, then restores everything. The strip/save/restore cycle is synchronous, so no timing issues arise.

### Usage

**From the Manager UI:**

1. Click a COMP's strategy cell to open the Actions popup
2. Click **Export portable tox**
3. Choose a save location in the file dialog

**Programmatically:**

```python
op.Embody.ExportPortableTox(target=some_comp, save_path='/path/to/output.tox')
```

Both `target` and `save_path` are optional — when omitted, `target` defaults to the Embody COMP itself and `save_path` defaults to `release/{name}-v{version}.tox`.

!!! warning "Absolute paths"
    Non-system absolute paths (not starting with `/sys/`) in `file` or `externaltox` parameters are logged as warnings but **not** stripped, since they may be intentional. Check the log output after exporting to ensure portability.

## Palette Handling During TDN Export

When a TDN export encounters a TD palette COMP (e.g. `abletonLink`, Widget components, anything under `Samples/Palette/`), Embody consults the `Tdnpalettehandling` parameter on the TDN page to decide how to handle it:

- **Ask** (default): Prompts with four buttons on first encounter of each palette COMP.
    - *Black Box* — this COMP: reference only, skip children. Decision stored on the COMP via `comp.store('_tdn_palette_handling', 'blackbox')`.
    - *Full Export* — this COMP: export all children. Decision stored on the COMP.
    - *Black Box for All*: flip the project-wide par to `Black Box`, ending future prompts.
    - *Full Export for All*: flip the project-wide par to `Full Export`.
- **Black Box**: Always reference the palette and emit `"palette_clone": true` without exporting internals. **Recommended for stock palette COMPs** — lets upstream palette updates from Derivative flow through on round-trip.
- **Full Export**: Always export all internals like a regular COMP. Use when you've heavily customized the palette internals and need that state preserved.

Per-COMP stored decisions take precedence over the project-wide par, so you can mix (most COMPs auto-use the par value; specific COMPs can override). To reset a stored decision, call `op('/path/to/palette_comp').unstore('_tdn_palette_handling')`.

Detection details and the shipped palette catalog are documented in [TDN Palette Clones](../tdn/specification.md#palette-clones).

## Resetting

To completely reset and remove externalizations, pulse the **Disable** button.

!!! info "Safe deletion"
    This will delete only the files that Embody created (tracked in the externalizations table). Any other files in the externalization folder will be preserved. Empty folders may be removed, but folders containing untracked files will not be touched.

Options when disabling:

- **Yes, keep Tags**: Remove externalizations but keep the tags on operators for easy re-enabling.
- **Yes, remove Tags**: Remove externalizations and all Embody tags from operators.
