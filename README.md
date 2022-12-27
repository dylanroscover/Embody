# :label: Embody
### Externalize TouchDesigner components and scripts
#### :floppy_disk: TouchDesigner 2022.31030 (Windows)
#### :floppy_disk: version 4.2.76

<img src='https://raw.githubusercontent.com/dylanroscover/Embody/master/img/screenshot1.jpg'>

## :notebook_with_decorative_cover: Overview
TouchDesigner stores project networks in a binary format, `.toe` (TouchDesigner environment file). For collaborative and source control workflows, this format has significant limitations. One can always save external `.tox` (TouchDesigner external component) files of any COMP in their network, as well as a plethora of text-based formats of DATs in their network (such as `.py`, `.glsl` and `.json`). However, these processes can quickly become repetitive and cumbersome to maintain as your network grows.

Embody is a management system for creating and maintaining comprehensive externalizations throughout your toe  network. Any COMP or DAT operators (OPs) in your project can be tagged for externalizing with the tap of a simple shortcut (pressing `y` twice while the OPs are selected). Then whenever you save your project (`ctrl - s` or `ctrl - shift - u` to avoid editing the toe), Embody will automatically externalize the OPs to a folder structure that matches your network, and keep them updated as you iterate.

Simply drag and drop Embody from the `/release` folder into your project to get started!

## :page_with_curl: Defaults
On creation Embody will prompt you to either re-initialize itself or keep it's last saved state. Typically you will want to re-init it for new networks. The default externalization Folder is `lib`, which usually resides in a folder relative to your project toe (inside `project.folder`). This can be set to any folder you want. When changed, Embody disables itself, removes it's previous externalization folder, and creates the new externalization folder structure.

Embody replicates your toe network structure for its external files. Each COMP in your toe network is treated as a folder. So if you externalize COMP `base2`, which exists inside of COMP `base1`, the external file path will be: `lib/base1/base2.tox`. Only COMPs that contain externalizations will be created as folders.

## :label: Getting Started
1. Add an externalization tag to any supported OP type. The simplest way to do this is to select your current operator (the green box around it, not yellow) and press `ctrl - alt - t`, and Embody will add the correct tag you specified in the Setup custom par page. Multiple OPs may be selected at a time.

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

2. Set your externalization folder, or use the default `lib` (relative to your project file, inside the `project.folder` folder)

3. Pulse the `Enable/Update` button. 

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

- `y - y` : Add an externalization tag automatically based on the current op selected (supports all COMP and saveable DAT operators).

- `ctrl - shift - u` : Initialize/update. If Embody is not enabled, will initialize so any detected tags become externalized and get saved. If it is enabled, will update so any detected changes ('dirty' COMPs) are saved out.

## :man_juggling: Contributors
Originally developed by [Tim Franklin](https://github.com/franklin113/). Forked and eventually almost completely refactored by yours truly. Inspired by Elburz's and Matthew Ragan's externalization work.

## Version History
- 4.2.0 - UI fixes, path cleanup, init fixes, folder switching fixes, switchover to absolute project path setup
- 4.1.0 - Better cleanup and moving of files/folders, removed nochildren tag, improved keyboard shortcuts, numerous bug fixes
- 4.0.0 - Added support for various web (json/xml/html), shader (glsl/frag/vert), text (txt/md/rtf) and table (csv/dat) file formats, various bug fixes and parameter simplifications/cleanups/improvements
- 3.0.5 - Tweaked reset function so externalization folder is created
- 3.0.4 - Updated versioning system
- 3.0.3 - Updated to TouchDesigner 2022 release 
- 3.0.2 - Added Manager UI, clarified command syntax and added deletion mechanisms
- 3.0.1 - Added keyboard shortcuts, subtraction for list elements, init and minor bug fixes 
- 3.0.0 - Initial release
