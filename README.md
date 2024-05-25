# :label: Embody
### Externalize TouchDesigner components and scripts
#### :floppy_disk: TouchDesigner 2023.11760 (Windows)
#### :floppy_disk: version 4.3.48

<img src='https://raw.githubusercontent.com/dylanroscover/Embody/master/img/screenshot1.jpg'>

## :notebook_with_decorative_cover: Overview
### Internalization
TouchDesigner stores projects in a binary file format, `.toe` (TOuch Environment). For collaborative and source control workflows, this file format can have significant limitations.

### Manual Externalization
To overcome these limitations, developers may save external `.tox` (TOuch eXternal component) files of COMPs in their network, as well as a plethora of text-based files of DATs in their network (such as `.py`, `.glsl` and `.json`). However, these techniques can quickly become repetitive and cumbersome to implement maintain as your network grows.

### Automated Externalization
Embody is a management system for creating and maintaining comprehensive externalizations throughout your project. Any COMP or DAT operators (OPs) in your project can be tagged for externalization, with the tap of a simple shortcut (pressing `y` twice while the OPs are selected). Then, whenever you save your project (`ctrl - s` or `ctrl - shift - u` to avoid versioning up the toe), Embody will externalize the OPs to files in a folder structure that matches your project network, and keep them updated as you iterate.

Each COMP in your toe network is treated as a folder. So if you externalize COMP `base2`, which exists inside of COMP `base1`, the external file path will be: `lib/base1/base2.tox`. Only COMPs that contain externalizations will be created as folders.

Simply drag-and-drop the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/master/release) folder into your project toe to get started!

## :label: Getting Started
1. Download, drag and drop the Embody `.tox` from the [`/release`](https://github.com/dylanroscover/Embody/tree/master/release) folder into your project toe

2. On creation, Embody will prompt you to either re-initialize itself or keep it's last saved state. Typically you will want to re-init it for new networks. The default externalization Folder is `lib`, which usually resides in a folder relative to your project toe (inside `project.folder`). This can be set to any folder you want. When the folder is moved, Embody disables itself, removes it's previous externalization folder, and re-creates the externalization folder structure.

3. Add an externalization tag to any supported OP type. The simplest way to do this is to select your current operator (the green box around it, not yellow) and press `ctrl - alt - t`, and Embody will add the correct tag you specified in the Setup custom par page. Multiple OPs may be selected and tagged together.

	The following OPs are supported:
	> - COMP
	> - Text DAT (including callbacks)
	> - Table DAT
	> - Execute DAT
	> - Parameter Execute DAT
	> - Panel Execute DAT
	> - OP Execute DAT

	The following file formats are supported:
	> - .tox
	> - .py
	> - .json
	> - .xml
	> - .html
	> - .glsl
	> - .frag
	> - .vert
	> - .txt
	> - .md
	> - .rtf
	> - .csv
	> - .dat

4. Pulse the `Enable/Update` button (or press `ctrl - shift - u`)

	This will search your entire project for COMPs and DATs matching the tags and externalize them with a folder structure matching that of your TouchDesigner project network.

	> Note: if no tags are specified, all project COMPs and DATs that can be externalized, will be added. Fair warning that this may stall systems with complex projects.

## :label: Workflow
As you work, Embody will keep your external toxes updated. Every time you save your project (`ctrl - s`), Embody checks to see if COMPs have been updated. If they have, it autosaves the dirty (modified) ones. DATs are automatically synchronized by TouchDesigner (if their Sync to File parameter is enabled).

> If `ctrl - s` isn't your thing, all good. `ctrl - shift - u` can be used in its place to update only dirty COMPs as you work. This is the same as pulsing the Initialize/Update button in the Setup page.

> If you want to see which COMPs are dirty, you can press `ctrl - shift - e` to bring up the Manager GUI. In it is a list of all externalized operators and their dirty status (if they are a COMP type). If you keep the Manager open while working, press the Refresh button to get the latest dirty status at any time, and then press the Update button to save out just the dirty COMPs.


## :label: Resetting
To reset ('unexternalize') completely, pulse the Disable button.

> Note: this can also delete all externalized files, their path parameters (`externaltox` and `syncfile`), and any empty folders that result. To reinstate them, pulse the Enable button again.

## :keyboard: Keyboard Shorcuts
- `ctrl - shift - e` :  Open the Manager, a lister of all externalized operators and their metadata. Inside this floating panel window you are able to delete externalizations and trigger basic commands, including:
	- Reset
	- Refresh
	- Initialize/Update
	- Open the custom pars as a floating panel window (Pars)

- `rShift - rShift` : Add an externalization tag automatically based on the current op selected (supports all COMP and saveable DAT operators) by pressing right shift key twice in succession.

- `ctrl - shift - u` : Initialize/update. If Embody is not enabled, will initialize so any detected tags become externalized and get saved. If it is enabled, will update so any detected changes ('dirty' COMPs) are saved out.

## :man_juggling: Contributors
Originally developed by [Tim Franklin](https://github.com/franklin113/). Forked and eventually almost completely refactored by yours truly. Inspired by Elburz's and Matthew Ragan's externalization work.

## Version History
- 4.3.48 - Added handling for duplicate OP tox/file paths (usually occurred after copying/pasting an OP in a network)
- 4.3.43 - Switched to UTC, add Save/Table DAT btns, toggle keyboard shortcuts, refactor tagging, add filter, better externaltox handling
- 4.2.101 - Fixed keyboard shortcut bug for adding tags, updated to TouchDesigner 2023
- 4.2.98 - Added handling for Cloners/Replicants via two new Custom Parameters (Externalize Child Clones/Replicants)
- 4.2.0 - UI fixes, path cleanup, init fixes, folder switching fixes, switchover to absolute project path setup
- 4.1.0 - Better cleanup and moving of files/folders, removed nochildren tag, improved keyboard shortcuts, numerous bug fixes
- 4.0.0 - Added support for various web (json/xml/html), shader (glsl/frag/vert), text (txt/md/rtf) and table (csv/dat) file formats, various bug fixes and parameter simplifications/cleanups/improvements
- 3.0.5 - Tweaked reset function so externalization folder is created
- 3.0.4 - Updated versioning system
- 3.0.3 - Updated to TouchDesigner 2022 release 
- 3.0.2 - Added Manager UI, clarified command syntax and added deletion mechanisms
- 3.0.1 - Added keyboard shortcuts, subtraction for list elements, init and minor bug fixes 
- 3.0.0 - Initial release
