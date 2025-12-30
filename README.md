# :label: Embody
### Externalize TouchDesigner Components and Scripts
#### :floppy_disk: TouchDesigner 2025.32050 (Windows/macOS)
#### :floppy_disk: version 4.7.14

[YouTube Demo/Tutorial](https://www.youtube.com/watch?v=lR3adD3Cw5s)

<img src='https://raw.githubusercontent.com/dylanroscover/Embody/refs/heads/main/img/screenshot.png'>

## :notebook_with_decorative_cover: Overview
### Internalization
TouchDesigner stores projects in a `.toe` (TOuch Environment) binary file, which poses limitations for collaborative workflows, especially when merging changes.

### Manual Externalization
Developers often save external `.tox` (TOuch eXternal component) files and various text-based DATs (e.g., `.py`, `.glsl`, `.json`). This process is repetitive and can become difficult to manage in larger networks.

### Automated Externalization
**Embody** automates the externalization of COMPs and DATs in your project. Tag any COMP or DAT operator by selecting it and pressing `lctrl` twice in a row. Upon saving your project (`ctrl + s`) or updating Embody (`ctrl + shift + u`), Embody externalizes tagged operators to a folder structure mirroring your project network, and keeps them updated.

For instance, externalizing `base2` within `base1` results in the path: `{project.folder}/base1/base2.tox`.

To get started, drag-and-drop the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/main/release) folder into your project.

## :label: Getting Started
1. **Download and Add Embody**: Drag and drop the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/main/release) folder into your project.

2. **Initialize Embody**: Upon creation, choose to initialize or keep the last saved state. Typically, use initialize for new projects, or keep the saved state if you're updating Embody from an older version. The default externalization folder is the root of your project folder. This can be customized (see [Folder Configuration](#folder-configuration)). Moving the folder disables Embody, which then recreates the folder structure in the new location.

3. **Tag Operators for Externalization**:
    - Select an operator and press `lctrl` twice to add the externalization tag desired.
    - Supported OP types:
        - All COMPs except engine, time and annotate
        - Text DAT
        - Table DAT
        - Execute DAT
        - Parameter Execute DAT
        - Parameter Group Execute DAT
        - CHOP Execute DAT
        - DAT Execute DAT
        - OP Execute DAT
        - Panel Execute DAT
    - Supported file formats:
        - .tox, .py, .json, .xml, .html, .glsl, .frag, .vert, .txt, .md, .rtf, .csv, .tsv, .dat

4. **Enable/Update Externalizations**:
    - Pulse the `Enable/Update` button or press `ctrl + shift + u`.
    - Embody externalizes tagged COMPs and DATs, matching your project network structure.

    > Note: If no tags are specified, all externalizable COMPs and DATs will be externalized, which might slow down complex projects.

## :label: Workflow
Embody keeps your external toxes updated. Saving your project (`ctrl + s`) autosaves modified (dirty) COMPs. DATs synchronize automatically if their Sync to File parameter is enabled.

> Use `ctrl + shift + u` as an alternative to update only dirty COMPs.

> Use `ctrl + alt + u` to save only the COMP you're currently working inside of (useful for large projects).

> To view dirty COMPs, press `ctrl + shift + e` to open the Manager UI, listing all externalized operators and their status. Refresh to get the latest dirties and update as needed.

### Parameter Change Detection
Embody tracks all parameter values on externalized COMPs. When any parameter changes (not just network edits), that COMP is automatically marked as dirty with a "Par" indicator. This ensures parameter tweaks are never lost, even if the COMP's network hasn't changed.

### Cross-Platform Compatibility
Embody normalizes all file paths to use forward slashes (`/`), which work on Windows, macOS, and Linux. This allows teams on mixed platforms to collaborate without path-related merge conflicts.

## :label: Features
- Adds and updates `Build Number`, `Touch Build` and `Build Date` parameters in an `About` page to any externalized COMP, for robust version tracking.
- Prompts whether to reference or clone an operator when a duplicate file path is detected (see [Duplicate Path Handling](#duplicate-path-handling)).
- Prevents clones and replicants (and their children) from being externalized.
- Can externalize the entire project in one click with the `Externalize Full Project` pulse.
- Isolated data/logic pattern with an `externalizations` tableDAT outside of Embody for easy updating and management.
- UTC timestamps for synchronized international workflows.
- Safe file deletion - only removes files Embody created, never deletes untracked files.
- Automatic parameter change detection marks COMPs dirty when any parameter is modified.

## :label: Manager UI
Press `ctrl + shift + e` to open the Manager window, which provides:

- **TreeLister View**: Hierarchical view of all externalized operators organized by path.
- **Status Indicators**: Shows dirty state for each operator (network changes or parameter changes marked as "Par").
- **Build Information**: Displays build number, TouchDesigner build, and timestamp for each externalized COMP.
- **Quick Actions**: 
  - Click to navigate to any operator
  - Open file location in your system file browser
  - Refresh to update dirty states
  - Filter/search through externalized operators

## :label: Externalizations Table
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

This table serves as the source of truth for what files Embody manages. Only files listed here will ever be deleted by Embody.

## :label: Folder Configuration
The externalization folder can be configured in several ways:

- **Static Path**: Set a folder name like `externals` to save to `{project.folder}/externals/`
- **Expression Mode**: Use Python expressions for dynamic paths (e.g., `project.folder + '/build_' + str(app.build)`)
- **Existing Folders**: You can point Embody at a folder containing other files - Embody will only manage its own tracked files and leave others untouched.

> Note: When changing the folder location, Embody will migrate tracked files to the new location and clean up empty directories in the old location.

## :label: Duplicate Path Handling
When Embody detects two operators pointing to the same external file, it prompts you with options:

- **Reference**: Both operators share the same external file. The new operator receives a `clone` tag and changes to either will affect the shared file.
- **Duplicate**: Create a new, separate externalization for the operator with its own file path.
- **Cancel**: Take no action.

Enable or disable this check with the `Detect Duplicate Paths` parameter.

## :label: Resetting
To completely reset and remove externalizations, pulse the `Disable` button.

> Note: This will delete only the files that Embody created (tracked in the externalizations table). Any other files in the externalization folder will be preserved. Empty folders may be removed, but folders containing untracked files will not be touched.

Options when disabling:
- **Yes, keep Tags**: Remove externalizations but keep the tags on operators for easy re-enabling.
- **Yes, remove Tags**: Remove externalizations and all Embody tags from operators.

## :keyboard: Keyboard Shortcuts
| Shortcut | Action |
|----------|--------|
| `ctrl + shift + e` | Open the Manager to view and manage externalized operators |
| `lctrl + lctrl` | Tag the current selected operator (press left control twice) |
| `ctrl + shift + u` | Initialize/update all externalizations |
| `ctrl + alt + u` | Save only the current COMP you're working inside |

## :wrench: Troubleshooting

### Debug Mode
For verbose path logging and troubleshooting, enable debug mode by setting `debug_mode = True` in the EmbodyExt extension. This will log detailed path information to the textport.

### Common Issues
- **Timeline Paused**: Embody requires the timeline to be running. A warning will appear in the textport if the timeline is paused.
- **Clone/Replicant Operators**: These cannot be externalized. Embody will show a warning if you try to tag them.
- **Engine COMPs**: Engine, time, and annotate COMPs are not supported for externalization.

## :man_juggling: Contributors
Originally developed by [Tim Franklin](https://github.com/franklin113/). Refactored entirely by Dylan Roscover, with inspiration and guidance from Elburz Sorkhabi, Matthew Ragan and Wieland Hilker.

## Version History
- **4.7.14**: Safe file deletion - Embody now only deletes files it created. Untracked files in the externalization folder are preserved during disable/migration operations.
- **4.7.11**: Cross-platform path handling (forward slashes on all platforms) + code cleanup
- **4.7.6**: Build save increment bug fix
- **4.7.5**:
    - ui.rolloverOp refactor
    - Restore handling of dnd COMP auto-populated externaltox pars
    - Cache parameters correctly between tox saves
    - Add parameter updated coloring for dirty buttons in UI
    - Path lib implementation improvements / added consistency
    - Auto refresh on UI maximize
    - Do not auto update when adding an externalization
    - Ignore untagged COMPs when checking for duplicate paths
- **4.6.4**:
    - Add About page to externalized COMPs with:
        - Build Number
        - Touch Build
        - Build Date (time tox was saved)
    - Add Build/Touch Build to externalization table + Lister
    - Window resizing support and cleaned up min/max button methods
- **4.5.23**: 
    - Fix deletion of old file storage after renaming operation
    - Cleanup network
    - Tagging optimization
    - Cleanup folder structure
    - Remove folderDAT
    - Fix duplicated rows from externalizations tsv git merge conflicts
- **4.5.19**: Allow master clones with clone pars to be externalized, Setup menu cleanup
- **4.5.17**: Bug fixes, smaller minimized window footprint
- **4.5.2**: 
    - Add tsv support
    - Add Clone tag for shared external paths
    - Handle drag and dropped COMP auto-populated externaltox pars
    - Detect dirty COMP par changes
- **4.4.128**: Add support for COMPs with empty/error prone clone expressions (such as rollovers in Probe)
- **4.4.127**: Added textport warning for when timeline is paused
- **4.4.126**: Clean up Save and dirtyHandler methods, auto set enableexternaltox par to ensure saves
- **4.4.125**: Bug fix for handling empty externalTimeStamp value
- **4.4.124**: More bug fixes with file handling
- **4.4.119**: mouseinCHOP chopexecDAT optimization
- **4.4.117**: Additional externalization folder removal bug fixes
- **4.4.116**: UI color and icon refinement
- **4.4.113**: externalization folder bug fixes
- **4.4.112**: engine/annotateCOMP Tagger handling
- **4.4.111**: Bug fix for Disable method
- **4.4.109**: Correctly deletes previous externalization folder when changed
- **4.4.107**: Multi-display support for Tagger, minor Windows fixes
- **4.4.104**: Added TreeLister, improved Tagger stability, color theme updates 
- **4.4.74**:
    - Added feature for externalizating full project automatically
    - Support for handling deletion and re-creation (redo) of COMPs/DATs
    - Support for renaming COMPs and DATs
    - Support for moving COMPs/DATs
    - Various small bug fixes and feature improvements
- **4.3.134**: Adding missing reference to list COMP
- **4.3.133**: Fixed externalizations folder button on macOS, fixed filter display, added clear button to filter UI
- **4.3.128**: Fixed abs path bug, added support for macOS Finder and keyboard shortcuts
- **4.3.122**: Separated logic/data for easier Embody updates, bug fix for checking for duplicate OPs
- **4.3.48**: Handling for duplicate OP tox/file paths.
- **4.3.43**: Switched to UTC, added Save/Table DAT buttons, refactored tagging, better externaltox handling.
- **4.2.101**: Fixed keyboard shortcut bug, updated to TouchDesigner 2023.
- **4.2.98**: Added handling for Cloners/Replicants.
- **4.2.0**: UI fixes, path cleanup, folder switching fixes.
- **4.1.0**: Improved file/folder management, bug fixes.
- **4.0.0**: Added support for various file formats, parameter improvements.
- **3.0.5**: Tweaked reset function.
- **3.0.4**: Updated versioning system.
- **3.0.3**: Updated to TouchDesigner 2022.
- **3.0.2**: Added Manager UI, clarified commands, added deletion mechanisms.
- **3.0.1**: Added keyboard shortcuts, minor bug fixes.
- **3.0.0**: Initial release.