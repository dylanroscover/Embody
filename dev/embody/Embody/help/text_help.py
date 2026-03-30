'''
Embody v5
===============

Embody provides robust automated externalization for
TouchDesigner projects. Any COMP or DAT operator(s) in your
project can be tagged by pressing left control twice in a row
while selected to automatically externalize it to a version-
control-friendly file (.tox, .py, .json, etc.).

Simply drag and drop the Embody .tox from the /release folder
into your project to get started!

Getting Started
---------------
1. Add the Embody .tox to your project
2. Tag operators for externalization (lctrl-lctrl)
3. Press ctrl-shift-u to initialize/update
4. Work as normal — externalized files are the source of truth

The default Tags are listed on the Tags page. These can be
customized, but please do so before you enable Embody.

To enable (initialize), add your specified Tag(s) to any
supported OP, set your externalization Folder, and pulse the
Enable/Update button. Embody will search your entire project
for tagged operators and externalize them with a folder
structure mirroring your TouchDesigner network.

You may also externalize all supported OPs in one step via
the "Externalize Full Project" button.

Supported Operators
-------------------
COMPs:
- All COMPs except engine, time and annotate

DATs:
- Text DAT
- Table DAT
- Execute DAT
- Parameter Execute DAT
- Parameter Group Execute DAT
- CHOP Execute DAT
- DAT Execute DAT
- OP Execute DAT
- Panel Execute DAT

Supported File Formats
----------------------
COMPs: .tox, .tdn (TDN strategy)
DATs:  .py, .json, .xml, .html, .glsl, .frag, .vert,
       .txt, .md, .rtf, .csv, .tsv, .dat

Workflow
--------
Embody keeps your external files up to date as you work.
Press ctrl-shift-u to save all dirty externalizations, or
ctrl-alt-u to save just the COMP you're currently inside.
DATs are automatically synchronized by TouchDesigner via
their Sync to File parameter.

Embody also tracks parameter changes on externalized COMPs.
When any parameter is modified, the COMP is marked dirty
with a "Par" indicator, ensuring parameter tweaks are never
lost.

Automatic Restoration
---------------------
You do not need to save your .toe file to preserve your
externalized work. On project open, Embody automatically
restores everything from the files on disk:

- TOX-strategy COMPs: Restored from .tox files if missing
  from the .toe (via the Toxrestoreonstart toggle)
- TDN-strategy COMPs: Children are reconstructed from .tdn
  JSON files (via the Tdncreateonstart toggle)
- DATs: Synced from their external files via TouchDesigner's
  native file parameter

Your externalized files on disk are the source of truth.
The .toe file is just a convenient container — all tagged
operators are fully recoverable from the external files.

All file paths are normalized to forward slashes for cross-
platform compatibility between Windows and macOS.

To reset ('unexternalize'), pulse the Disable button. This
deletes only files tracked by Embody. Untracked files in
the externalization folder are preserved.

Export Portable Tox
-------------------
Export any COMP as a self-contained .tox file with all
external file references and Embody tags stripped. The
exported .tox works when loaded into any TouchDesigner
project with no missing file errors and no Embody metadata.

Use via the Actions menu in the Manager UI (click a COMP's
strategy cell, then click "Export portable tox"), or call
programmatically: op.Embody.ExportPortableTox(target, path)

Non-system absolute paths are warned about but not stripped.

Envoy (MCP Server)
---------------------
Embody includes Envoy, an MCP (Model Context Protocol)
server that enables AI coding assistants to interact with TouchDesigner
programmatically. When enabled, Envoy lets you:

- Create, modify, connect, and query operators
- Read and write DAT content
- Manage Embody externalizations
- Execute Python code in TouchDesigner
- Export/import networks via the TDN format

To enable: toggle the Envoyenable parameter ON. The server
starts on port 9870 by default and auto-creates a .mcp.json
file in your project root for AI coding assistants to discover.

You can regenerate Envoy config files at any time:
  op.Embody.InitEnvoy()  — MCP + AI client config
  op.Embody.InitGit()    — git repo + .gitignore/.gitattributes

TDN Network Format
------------------
Embody can export and import TouchDesigner networks as human-
readable .tdn JSON files. This captures operators, parameters,
connections, and layout in a diffable format.

Use ctrl-shift-e to export the full project, or ctrl-alt-e
to export just the current network.

Cascade TDN to child COMPs (Tdncascade toggle on TDN page):
When enabled, tagging a COMP for TDN automatically tags all
child COMPs too, so each gets its own .tdn file. This keeps
individual files small and git-friendly instead of producing
one large monolithic .tdn. Parent files store lightweight
tdn_ref pointers to each child's .tdn file.

Manager UI
----------
Press ctrl-shift-o to open the Manager, a TreeLister of all
externalized operators and their metadata. From here you can:
- View dirty state and build info for each operator
- Navigate to any operator by clicking
- Open file locations in your system file browser
- Refresh, filter, and search externalizations
- Trigger Initialize/Update or Reset

Keyboard Shortcuts
------------------
ctrl-shift-o :   Open the Manager UI
lctrl-lctrl :    Tag or manage the operator under the cursor
                 (shows Actions menu for already-tagged operators)
ctrl-shift-u :   Initialize/update all externalizations
ctrl-alt-u :     Save only the current COMP you are inside
ctrl-shift-e :   Export the full project network to .tdn
ctrl-alt-e :     Export the current network to .tdn

'''