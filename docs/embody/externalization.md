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

COMPs can also be externalized using the **TDN strategy** instead of `.tox`. This exports the COMP's network as human-readable JSON (`.tdn` files) instead of binary `.tox` files, enabling meaningful git diffs.

With TDN strategy:

- **On update** (++ctrl+shift+u++): The COMP's children are exported to a `.tdn` file
- **On project save** (++ctrl+s++): Children are **stripped from the `.toe`** to keep it small, then restored immediately after save completes. This means the `.toe` does not contain TDN children — they live entirely in `.tdn` files on disk.
- **On project open**: Children are automatically reconstructed from the `.tdn` file
- **In git**: You see readable JSON diffs instead of binary changes

!!! important "Always update externalizations before saving the .toe"
    Since ++ctrl+s++ strips TDN children from the `.toe`, always press ++ctrl+shift+u++ first to ensure your `.tdn` files are up to date. If TD crashes mid-save, the `.tdn` files are what Embody uses to reconstruct your work.

!!! warning "Locked TOPs, CHOPs, and SOPs lose their frozen data"
    TDN cannot store frozen pixel, channel, or geometry data. If your network contains locked non-DAT operators, their lock flag is preserved but their content will be **empty after reload**. Embody warns you at save time when this is detected. Use **TOX strategy** instead of TDN for COMPs that contain locked TOPs, CHOPs, or SOPs. See [Lock Flag Limitation](../tdn/specification.md#lock-flag-limitation) for details.

See [TDN Format](../tdn/index.md) for more details.

### DAT Content Safety

When you save a project (++ctrl+s++), Embody checks for **unprotected DATs** inside TDN-managed COMPs — DATs that contain content but are neither externalized (no Embody tag) nor embedded (the **Embed DATs in TDNs** parameter is OFF). These DATs would lose their content during the TDN strip/restore save cycle.

If at-risk DATs are found, Embody prompts you with four options:

| Button | Behavior |
|--------|----------|
| **Externalize** | Tag and externalize the at-risk DATs so their content is saved to files on disk |
| **Skip** | Proceed with the save — content may be lost |
| **Always Externalize** | Externalize now, and do so automatically on future saves without asking |
| **Never Ask** | Suppress the check permanently |

The preference is stored in the **DAT Safety** parameter (`Tdndatsafety`) and can be changed at any time from the Embody COMP's TDN settings.

!!! tip
    To avoid this prompt entirely, either enable **Embed DATs in TDNs** (stores DAT content directly in the `.tdn` file) or externalize your DATs with Embody tags before saving.

## Automatic Restoration

Embody automatically restores all externalized operators when a project is opened. Your externalized files on disk are the source of truth — you do not need to save your `.toe` file to preserve externalized work.

| Strategy | Restoration Method | Toggle |
|----------|-------------------|--------|
| **TOX** | Missing COMPs are restored from `.tox` files on disk | `Toxrestoreonstart` (ON by default) |
| **TDN** | Children are reconstructed from `.tdn` JSON files | `Tdncreateonstart` |
| **DAT** | Synced from external files via TouchDesigner's native `file` parameter | Always active |

This means that even if you never save your `.toe` file after externalizing, all tagged operators are fully recoverable from the files on disk the next time you open the project.

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
