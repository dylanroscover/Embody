
# :label: Embody
### Externalize TouchDesigner Components and Scripts
#### :floppy_disk: TouchDesigner 2023.12000 (Windows/macOS)
#### :floppy_disk: version 4.4.104

<img src='https://raw.githubusercontent.com/dylanroscover/Embody/master/img/screenshot2.jpg'>

## :notebook_with_decorative_cover: Overview
### Internalization
TouchDesigner stores projects in a `.toe` (TOuch Environment) binary file, which poses limitations for collaborative workflows, especially when merging changes.

### Manual Externalization
Developers often save external `.tox` (TOuch eXternal component) files and various text-based DATs (e.g., `.py`, `.glsl`, `.json`). This process is repetitive and can become difficult to manage in larger networks.

### Automated Externalization
**Embody** automates the externalization of COMPs and DATs in your project. Tag any COMP or DAT operator by selecting it and pressing `lctrl` twice in a row. Upon saving your project (`ctrl + s` or `ctrl + shift + u`), Embody externalizes tagged operators to a folder structure mirroring your project network.

For instance, externalizing `base2` within `base1` results in the path: `lib/base1/base2.tox`.

To get started, drag-and-drop the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/master/release) folder into your project.

## :label: Getting Started
1. **Download and Add Embody**: Drag and drop the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/master/release) folder into your project.

2. **Initialize Embody**: Upon creation, choose to re-initialize or keep the last saved state. Typically, re-initialize for new networks. The default externalization folder is `lib` within your project folder. This can be customized. Moving the folder disables Embody, which then recreates the folder structure.

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
        - .tox, .py, .json, .xml, .html, .glsl, .frag, .vert, .txt, .md, .rtf, .csv, .dat

4. **Enable/Update Externalizations**:
    - Pulse the `Enable/Update` button or press `ctrl + shift + u`.
    - Embody externalizes tagged COMPs and DATs, matching your project network structure.

    > Note: If no tags are specified, all externalizable COMPs and DATs will be externalized, which might slow down complex projects.

## :label: Workflow
Embody keeps your external toxes updated. Saving your project (`ctrl + s`) autosaves modified (dirty) COMPs. DATs synchronize automatically if their Sync to File parameter is enabled.

> Use `ctrl + shift + u` as an alternative to update only dirty COMPs.

> To view dirty COMPs, press `ctrl + shift + e` to open the Manager UI, listing all externalized operators and their status. Refresh and update as needed.

## :label: Resetting
To completely reset and remove externalizations, pulse the `Disable` button.

> Note: This can delete all externalized files, their path parameters (`externaltox` and `syncfile`), and any resulting empty folders. To reinstate, pulse the `Enable/Update` button.

## :keyboard: Keyboard Shortcuts
- `ctrl + shift + e` : Open the Manager to view and manage externalized operators.
- `lshift + lshift` : Tag the current selected operator by pressing the right shift key twice.
- `ctrl + shift + u` : Initialize/update externalizations.

## :man_juggling: Contributors
Originally developed by [Tim Franklin](https://github.com/franklin113/). Refactored entirely by Dylan Roscover, with inspiration and guidance from Elburz Sorkhabi, Matthew Ragan and Wieland Hilker.

## Version History
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
