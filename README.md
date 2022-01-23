# :printer: Externalizer
### A tool for externalizing your tox and dat (python) files in TouchDesigner. 
#### :floppy_disk: TouchDesigner 2021.15800 Win 10 x64
#### :floppy_disk: Current version 3.0.1

## :notebook_with_decorative_cover: Overview
This component provides simple and reliable externalization for TouchDesigner projects.

## :page_with_curl: Defaults
By default this component will open in Disabled mode and have no effect on your project. The default externalization Folder is 'lib', which must reside in a folder relative to your project toe (inside project.folder).

The default Tags for Toxes and DATs are 'tox' and 'dat' strings, respectively. The default tag for not creating folders for each tox (in which their children can go into) is 'nochildren'.

## :label: Getting Started
To initialize, add your specified Tag(s) to any supported OP type, set your externalization Folder, and pulse the Initialize button. This will search your entire project for COMPs matching the tags and externalize them with a folder structure matching that of your TouchDesigner project network.

The following OPs are supported:
- COMP
- Text DAT (including callbacks)
- Table DAT
- Execute DAT
- Parameter Execute DAT
- Panel Execute DAT
- OP Execute DAT

Note: if no tags are specified, all project COMPs and DATs that can be externalized, will be added. Fair warning that this may stall systems with complex projects.

## :label: Workflow
As you work, Externalizer will keep your external toxes updated. Every time you save your project (ctrl-s), Externalizer checks to see if COMPs have been updated. If they have, it autosaves the dirty (modified) ones. DATs are automatically synchronized by TouchDesigner (if their Sync to File parameter is enabled).

## :label: Resetting
To reset ('unexternalize') completely, pulse the Reset button.

Note: this will also delete all externalized files and any empty folders that result. To reinstate them, pulse the Initialize button again.

## :keyboard: Keyboard Shorcuts
- `ctrl-alt-e` : View a list of all externalized operators and 
their metadata.

- `ctrl-alt-t` : Add an externalization tag automatically based
on the current op selected (supports all COMP and saveable 
DAT operators).

- `ctrl-alt-n` : Add a 'no children' tag automatically based
on the current op selected (supports COMP operators only).

## :man_juggling: Contributors
Originally developed by [Tim Franklin](https://github.com/franklin113/). Forked, added onto and eventually almost completely refactored by me. Inspired by Elburz and Matthew Ragan's externalization work.

## Version History
- 3.0.1 - Added keyboard shortcuts, subtraction for list elements, init and minor bug fixes 
- 3.0.0 - Initial release
