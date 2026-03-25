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

When Embody detects two operators pointing to the same external file, it prompts you with options:

- **Reference**: Both operators share the same external file. The new operator receives a `clone` tag and changes to either will affect the shared file.
- **Duplicate**: Create a new, separate externalization for the operator with its own file path.
- **Cancel**: Take no action.

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

- **On save** (++ctrl+shift+u++): The COMP's children are exported to a `.tdn` file
- **On project save** (++ctrl+s++): Children are **stripped from the `.toe`** to keep it small, then restored immediately after save completes. This means the `.toe` does not contain TDN children — they live entirely in `.tdn` files on disk.
- **On project open**: Children are automatically reconstructed from the `.tdn` file
- **In git**: You see readable JSON diffs instead of binary changes

!!! important "Always save externalizations before saving the .toe"
    Since ++ctrl+s++ strips TDN children from the `.toe`, always press ++ctrl+shift+u++ first to ensure your `.tdn` files are up to date. If TD crashes mid-save, the `.tdn` files are what Embody uses to reconstruct your work.

!!! warning "Locked TOPs, CHOPs, and SOPs lose their frozen data"
    TDN cannot store frozen pixel, channel, or geometry data. If your network contains locked non-DAT operators, their lock flag is preserved but their content will be **empty after reload**. Embody warns you at save time when this is detected. Use **TOX strategy** instead of TDN for COMPs that contain locked TOPs, CHOPs, or SOPs. See [Lock Flag Limitation](../tdn/specification.md#lock-flag-limitation) for details.

See [TDN Format](../tdn/index.md) for more details.

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

## Resetting

To completely reset and remove externalizations, pulse the **Disable** button.

!!! info "Safe deletion"
    This will delete only the files that Embody created (tracked in the externalizations table). Any other files in the externalization folder will be preserved. Empty folders may be removed, but folders containing untracked files will not be touched.

Options when disabling:

- **Yes, keep Tags**: Remove externalizations but keep the tags on operators for easy re-enabling.
- **Yes, remove Tags**: Remove externalizations and all Embody tags from operators.
