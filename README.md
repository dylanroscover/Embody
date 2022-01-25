# :printer: Externalizer
### A tool for externalizing your tox and dat (python) files in TouchDesigner. 
#### :floppy_disk: TouchDesigner 2021.15800 Win 10 x64
#### :floppy_disk: Current version 3.0.2

## :notebook_with_decorative_cover: Overview
Externalizer provides simple yet robust externalization for TouchDesigner projects. It uses a tag-based approach to identify which operators in your project should be externalized, versus ones that should not. It includes a lister and keyboard shortcuts for management and smooth production implementation.

<img src='https://raw.githubusercontent.com/dylanroscover/Externalizer/main/screenshot1.jpg' width='700'>

## :page_with_curl: Defaults
By default this component will open in Disabled mode and have no effect on your project. The default externalization Folder is `lib`, which must reside in a folder relative to your project toe (inside `project.folder`).

The default Tags for Toxes and DATs are `'tox'` and `'dat'` strings, respectively. The default tag for not creating folders for each tox (in which their children can go into) is `'nochildren'`. Externalizer automatically uses the same folder structure as your network for externalizing files.

## :label: Getting Started
1. Add an externalization tag to any supported OP type. The simplest way to do this is to select your current operator (the green box around it, not yellow) and press `ctrl - alt - t`, and Externalizer will add the correct tag you specified in the Setup custom par page.

> The following OPs are supported:
> - COMP
> - Text DAT (including callbacks)
> - Table DAT
> - Execute DAT
> - Parameter Execute DAT
> - Panel Execute DAT
> - OP Execute DAT

2. Set your externalization Folder, or use the default `lib` (relative to your project file, the `project.folder` folder)

3. Pulse the Initialize button. 

This will search your entire project for COMPs and DATs matching the tags and externalize them with a folder structure matching that of your TouchDesigner project network.

> Note: if no tags are specified, all project COMPs and DATs that can be externalized, will be added. Fair warning that this may stall systems with complex projects.

## :label: Workflow
As you work, Externalizer will keep your external toxes updated. Every time you save your project (`ctrl - s`), Externalizer checks to see if COMPs have been updated. If they have, it autosaves the dirty (modified) ones. DATs are automatically synchronized by TouchDesigner (if their Sync to File parameter is enabled).

> If `ctrl - s` isn't your thing, all good. `ctrl - alt - u` can be used in its place to update only dirty COMPs as you work. This is the same as pulsing the Initialize/Update button in the Setup page.

> If you want to see which COMPs are dirty, you can press `ctrl - alt - e` to bring up the Manager GUI. In it is a list of all externalized operators and their dirty status (if they are a COMP type). If you keep the Manager open while working, press the Refresh button to get the latest dirty status at any time, and then press the Update button to save out just the dirty COMPs.


## :label: Resetting
To reset ('unexternalize') completely, pulse the Reset button.

> Note: this will also delete all externalized files, their path parameters (`externaltox` and `syncfile`), and any empty folders that result. To reinstate them, pulse the Initialize button again.

## :keyboard: Keyboard Shorcuts
- `ctrl - alt - e` :  Open the Manager, a lister of all externalized operators and their metadata. Inside this floating panel window you are able to delete externalizations and trigger basic commands, including:
	- Reset
	- Refresh
	- Initialize/Update
	- Open the custom pars as a floating panel window (Pars)

- `ctrl - alt - t` : Add an externalization tag automatically based on the current op selected (supports all COMP and saveable 
DAT operators).

- `ctrl - alt - n` : Add a 'no children' tag automatically based on the current op selected (supports COMP operators only).

- `ctrl - alt - u` : Initialize/update. If Externalizer is not enabled, will initialize so any detected tags become externalized and get saved. If it is enabled, will update so any detected changes ('dirty' COMPs) are saved out.

## :man_juggling: Contributors
Originally developed by [Tim Franklin](https://github.com/franklin113/). Forked, added onto and eventually almost completely refactored by me. Inspired by Elburz's and Matthew Ragan's externalization work.

## Version History
- 3.0.2 - Added Manager UI (ctrl-alt-e), clarified command syntax and added deletion mechanisms
- 3.0.1 - Added keyboard shortcuts, subtraction for list elements, init and minor bug fixes 
- 3.0.0 - Initial release
