'''
Embody v4
===============

Embody provides simple yet robust file externalization for 
TouchDesigner projects. Any COMP or DAT operator(s) in your 
project can be tagged by pressing left control twice in a row
while selected (left click) to automatically externalize it.

Embody includes a manager UI which lists your externalized
files and info about them. Simply drag and drop Embody from 
the /release folder into your project to get started!

The default Tags are listed on the Tags page. These can be
customized, but please do so before you enable Embody.

To enable (initialize), add your specified Tag(s) to any 
supported OP, set your externalization Folder, and pulse the 
Enable/Update button. This will search your entire project for 
COMPs matching the tags and externalize them with a folder structure matching 
that of your TouchDesigner project network.

You may also search your full project for all supported OPs 
(most DATs and COMPs) and add them all automatically via the 
"Externalize Full Project" button.

The following OPs are supported:
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

The following file formats are supported:
COMPs
- .tox
DATs
- .py
- .json
- .xml
- .html
- .glsl
- .frag
- .vert
- .txt
- .md
- .rtf
- .csv
- .tsv
- .dat

As you work, Embody will keep your external toxes updated.
Every time you save your project (ctrl-s), Embody checks
to see if COMPs have been updated. If they have, it autosaves 
the dirty (modified) ones. DATs are automatically synchronized 
by TouchDesigner (if their Sync to File parameter is enabled).
You may also update Embody via ctrl-shift-u.

To reset ('unexternalize') completely, pulse the Disable button.

Note: this will also delete all externalized files and any 
empty folders that result. To reinstate them, pulse the
Enable/Update button again.

Keyboard Shortcuts
ctrl-shift-e : Open the manager, a lister of all externalized 
operators and their metadata. Inside this floating panel 
window you are able to delete externalizations and trigger 
basic commands, including:
- Reset
- Refresh
- Initialize/Update
- Open the custom pars as a floating panel window (Pars)

lctrl-lctrl : Add an externalization tag automatically based
on the current op selected (supports all COMP and saveable 
DAT operators).

ctrl-shift-u: Initialize/update. If Embody is not enabled, 
will initialize so any detected tags become externalized 
and get saved. If it is enabled, will update so any detected 
changes ('dirty' COMPs) are saved out.

'''