# Parameter Reference

Complete reference for every custom parameter on the **Embody** COMP, grouped by parameter page. For a guided tour of the settings that matter most, see [Configuration](configuration.md).

<!-- GENERATED FILE - do not edit by hand. Regenerate with: python dev/embody/tools/generate_parameters_doc.py -->

!!! info "Auto-generated from `Embody.tdn`"
    This page is generated from the externalized Embody COMP (`dev/embody/Embody.tdn`), the source of truth for its parameters, so it stays in sync with the actual component. **96 parameters** across 7 pages.

## Embody

| Parameter | Type | Default | Description |
|---|---|---|---|
| `Status` | Str (read-only) | `Enabled` | Current Embody status (read-only). Shows whether Embody is initialized and running. |
| `Disable` | Pulse | - | Disables Embody: removes all externalization tags from operators and stops tracking. Use Update to re-enable. |
| Enable/Update (`Update`) | Pulse | - | Initializes Embody if not yet running, or triggers a full update cycle: scans all tagged operators, checks continuity, and syncs the externalizations table. |
| `Refresh` | Pulse | - | Refreshes the externalizations table by re-scanning tagged operators without triggering saves or continuity checks. |
| Perform Mode (`Performmode`) | Toggle | - | - |
| Create Externalizations Table (`Createexternalizationstable`) | Pulse | - | Creates the externalizations tracking table as a sibling operator if it does not already exist. Safe to pulse at any time - no-op if the table is already connected. Use this to recover if the table was lost after an Embody upgrade. |
| Externalizations Table (`Externalizations`) | DAT | `/embody/externalizations` | Reference to the externalizations tracking table DAT (stored in TSV format). Lists all externalized operators with their paths, types, timestamps, and dirty state. |
| Open Externalizations Table (`Openexternalizationstable`) | Pulse | - | Opens the externalizations tracking table in a floating viewer window. |
| Externalizations Folder (`Folder`) | Folder | - | Root folder where externalized files are saved. All .tox, .py, .json, and other externalized files are written relative to this folder. |
| Externalize Full Project (`Externalizeproject`) | Pulse | - | Tags all eligible operators in the entire project for externalization. COMPs get .tox tags, DATs get file-type tags based on their content. |
| Detect Duplicate Paths (`Detectduplicatepaths`) | Toggle | On | When enabled, warns if multiple operators resolve to the same externalized file path. Helps catch naming conflicts. |
| Template Master Name (`Templatemaster`) | Str | `__template__` | When duplicate external paths are detected, an operator whose path contains a folder/COMP with this exact name is auto-selected as the master (the rest are tagged as clones), with no prompt. Matches only when exactly one operator in the group contains this name. Clear this field to always choose the master manually, or set it to your own naming convention (e.g. '_master'). |
| Local Timestamps (`Localtimestamps`) | Toggle | On | When enabled, timestamps in the externalizations table are displayed in your local timezone instead of UTC. |
| Restore toxes on Start (`Toxrestoreonstart`) | Toggle | On | When enabled, automatically restores TOX-strategy COMPs from .tox files on disk when the project opens. Useful when COMPs were externalized but the .toe was not saved. |
| Restore DATs on Start (`Datrestoreonstart`) | Toggle | On | When enabled, automatically restores DAT-strategy operators from externalized files on disk when the project opens. Recreates missing DATs (py, json, xml, etc.) and configures file sync. Safely excludes Embody descendants and DATs inside TOX/TDN COMPs. |
| File Cleanup (`Filecleanup`) | Menu | `ask` | When a deleted operator's external file still exists on disk: Ask prompts each time, Always Keep removes the tracking entry but leaves files on disk, Always Delete removes both the entry and the file. Options: `Ask`, `Always Keep Files`, `Always Delete Files`. |
| Content Safety (`Tdndatsafety`) | Menu | `ask` | What to do when TDN COMPs contain DATs or storage that will be lost on save. 'Ask Each Save' prompts on each save (recommended), 'Always Externalize' auto-externalizes at-risk DATs without asking, 'Never Ask' suppresses the check entirely. Prefer 'Ask' or 'Always Externalize' -- 'Never Ask' is an opt-in escape hatch for power users who accept the risk. Options: `Ask Each Save`, `Always Externalize`, `Never Ask`. |
| Open Manager (`Openmanager`) | Pulse | - | Opens the Embody Manager window, which provides a list view of all externalized operators with status, actions, and strategy management. |
| Close Manager (`Closemanager`) | Pulse | - | Closes the Embody Manager window. |

## Tags

| Parameter | Type | Default | Description |
|---|---|---|---|
| tox Tags (`Toxtags`) | Header | - | Tag strings and operator colors for TOX externalization. COMPs tagged with the Tox tag are externalized as .tox files. |
| tox Tag Color (`Toxtagcolorr`) | RGBA | - | Color applied to tox-tagged COMP operators in the network editor. Makes externalized COMPs visually identifiable. |
| .tox Tag (`Toxtag`) | Str | `tox` | Tag string applied to COMP operators for .tox externalization. Operators with this tag are externalized as .tox files. |
| DAT Tags (`Dattags`) | Header | - | Tag strings and operator colors for DAT externalization. DATs are tagged with format-specific tags (py, json, xml, etc.) and externalized to matching file types. |
| DAT Tag Color (`Dattagcolorr`) | RGBA | - | Color applied to DAT-tagged operators in the network editor. Makes externalized DATs visually identifiable. |
| Clone Tag Color (`Clonetagcolorr`) | RGBA | - | Color applied to clone-tagged operators in the network editor. Distinguishes cloned externalized operators from originals. |
| .py Tag (`Pytag`) | Str | `py` | Tag string for .py (Python) externalization. Applied to text DATs containing Python code. |
| .json Tag (`Jsontag`) | Str | `json` | Tag string for .json externalization. Applied to text DATs containing JSON data. |
| .xml Tag (`Xmltag`) | Str | `xml` | Tag string for .xml externalization. Applied to text DATs containing XML data. |
| .html Tag (`Htmltag`) | Str | `html` | Tag string for .html externalization. Applied to text DATs containing HTML content. |
| .glsl Tag (`Glsltag`) | Str | `glsl` | Tag string for .glsl externalization. Applied to text DATs containing GLSL shader code. |
| .frag Tag (`Fragtag`) | Str | `frag` | Tag string for .frag externalization. Applied to text DATs containing fragment shader code. |
| .vert Tag (`Verttag`) | Str | `vert` | Tag string for .vert externalization. Applied to text DATs containing vertex shader code. |
| .txt Tag (`Txttag`) | Str | `txt` | Tag string for .txt externalization. Applied to text DATs containing plain text. |
| .md Tag (`Mdtag`) | Str | `md` | Tag string for .md externalization. Applied to text DATs containing Markdown content. |
| .rtf Tag (`Rtftag`) | Str | `rtf` | Tag string for .rtf externalization. Applied to text DATs containing rich text format content. |
| .csv Tag (`Csvtag`) | Str | `csv` | Tag string for .csv externalization. Applied to table DATs with comma-separated values. |
| .tsv Tag (`Tsvtag`) | Str | `tsv` | Tag string for .tsv externalization. Applied to table DATs with tab-separated values. |
| .dat Tag (`Dattag`) | Str | `dat` | Tag string for .dat externalization. Generic DAT externalization format. |
| TDN Tags (`Tdntags`) | Header | - | Tag strings and operator colors for TDN externalization. COMPs tagged with the TDN tag have their children exported as human-readable .tdn JSON network files. |
| TDN Tag Color (`Tdntagcolor`) | RGBA | `0.3` | Color applied to TDN-tagged COMP operators in the network editor. Makes TDN-strategy COMPs visually identifiable. |
| .tdn Tag (`Tdntag`) | Str | `tdn` | Tag string for .tdn externalization. Applied to COMPs whose children are exported as human-readable JSON network files. |
| .tdn Exclude Tag (`Tdnexcludetag`) | Str | `tdn_exclude` | Tag name (default 'tdn_exclude') that opts a COMP out of the entire TDN system. Primary use: opt a single child out of cascade autotag (when Tdncascade is on, the tdn tag normally propagates from a tdn-tagged parent to all its children -- apply this tag to a child to keep it excluded). Also useful for app-managed COMPs (e.g. spawned via .copy() at runtime) that the application is responsible for. Excluded COMPs are invisible to TDN: never exported, never inlined in parent .tdn files, never stripped on save, never destroyed by reconstruction. |

## TDN

| Parameter | Type | Default | Description |
|---|---|---|---|
| TDN Mode (`Tdnmode`) | Menu | `export` | Three-mode master for the TDN subsystem. Off = no TDN runtime (.tdn files on disk are preserved, Embody simply stops touching them). Export-on-Save (recommended) = write .tdn on save, .toe stays the source of truth, MCP reads live state via read_tdn. Roundtrip (Experimental) = bidirectional strip/restore of COMPs at save and open; may hit edge cases with extension reload timing on deeply-nested TDN COMPs. Options: `Off`, `Export-on-Save`, `Roundtrip (Experimental)`. |
| Clipboard Auto-Paste (`Clipboardautopaste`) | Toggle | On | When on, Embody watches the OS clipboard and prompts to paste an Embody network the moment one is copied (e.g. the web "embody it" button) -- no keyboard shortcut needed. Turn off to disable the automatic prompt. |
| Cascade to Children (`Tdncascade`) | Toggle | - | When tagging a COMP for TDN, automatically tag all child COMPs so each gets its own .tdn file. |
| Large TDN Warning (`Tdncascadewarn`) | Menu | `ask` | Controls whether a warning dialog is shown when a TDN file exceeds 5 MB. Set automatically when dismissed. Options: `Ask`, `Quiet`. |
| Embed DATs (default) (`Embeddatsintdns`) | Toggle | - | When enabled, DAT content (text and table data) is included in TDN exports by default. Can be overridden per-export. Disabling reduces file size but loses DAT content. |
| Embed Storage (default) (`Embedstorageintdns`) | Toggle | On | Include Python storage entries in TDN exports. Disable for COMPs with large storage to reduce file size. Can be overridden per-COMP from the tagging menu. |
| Auto-Save Checkpoints (`Autosave`) | Toggle | On | When on, after the agent (or you) goes idle Embody writes changed TDN COMPs to disk as a cheap ~6ms .tdn checkpoint -- NO full project save, no strip/restore, no freeze -- so a crash loses little unsaved work. Checkpointed COMPs are rebuilt on next open. Skips during Perform Mode and project saves. |
| TDN Create on Start (`Tdncreateonstart`) | Toggle | On | When enabled, reconstructs TDN-strategy COMP children from .tdn files on disk when the project opens. Required for TDN round-trip workflow where children are stripped on save. |
| Auto-Save Status (`Autosavestatus`) | Str (read-only) | `Saved 00:52:53 UTC` | Read-only auto-save state: Idle / Saving / Bypassed (Perform Mode) / Disabled. |
| Strip on Save (`Tdnstriponsave`) | Toggle | On | When enabled, strips children from TDN-strategy COMPs during project save. Children are recreated on next open via TDN Create on Start. Keeps .toe files minimal. |
| Palette Handling (`Tdnpalettehandling`) | Menu | `ask` | How to handle TD palette COMPs (e.g. abletonLink, Widget components) during TDN export. Ask: prompt on first encounter per COMP. Black Box: reference the palette and skip internal children -- the correct choice for stock palette components. Full Export: export all internals -- use when the palette COMP has been heavily customized internally. Options: `Ask`, `Black Box`, `Full Export`. |
| TDN File (`Tdnfile`) | File | - | Path to a .tdn file to import. Select a file, set the Network Path target, then pulse Import TDN. |
| Network Path (`Networkpath`) | COMP | - | Target COMP where the TDN file will be imported. Operators from the .tdn are created as children of this COMP. |
| Import TDN (`Importtdn`) | Pulse | - | Imports the specified TDN File into the Network Path target. Creates all operators, sets parameters, connections, and positions. |

## Envoy

| Parameter | Type | Default | Description |
|---|---|---|---|
| AI Client (`Aiclient`) | Menu | `0` | Selects the AI client used with Envoy. Determines which MCP configuration file format is generated (e.g. .mcp.json for Claude Code). Options: `Claude Code`, `Codex`, `Gemini`, `Cursor`, `Windsurf`, `GitHub Copilot`, `None`. |
| Launch AI Client (`Launchaiclient`) | Pulse | - | Open the AI client selected above at the project root: editors (Cursor, Windsurf; GitHub Copilot opens VS Code) open the folder as a workspace; CLI tools (Claude Code, Codex, Gemini) open in a new terminal at the root. |
| AI Project Root (`Aiprojectroot`) | Menu | `gitroot` | Where Embody writes AI config (AGENTS.md, CLAUDE.md, .claude/, .cursor/, .mcp.json, .embody/). "Git root" (default) writes at the top of the git repo. "Project folder" writes alongside the .toe -- use this when your TD project lives in a subdirectory of a larger repo and you open that subdirectory as your AI tool workspace. Options: `Git root`, `Project folder (.toe directory)`, `Custom`. |
| AI Project Root (Custom) (`Aiprojectrootcustom`) | Folder | - | Custom directory for AI/MCP config when "AI Project Root" is set to "Custom". Relative paths are resolved against the .toe directory (e.g. "../" places config one level above the .toe -- useful when multiple TouchDesigner projects share a parent folder in a monorepo). Absolute paths are used as-is. Changing this path migrates Embody state and AI config to the new location. |
| Envoy Enable (`Envoyenable`) | Toggle | On | Enables/disables the Envoy MCP server. When enabled, starts an HTTP server that exposes TouchDesigner operations to Claude Code and other MCP clients. |
| Envoy Port (`Envoyport`) | Int | `9876` | Port number for the Envoy MCP server (default: 9870). Changing while running automatically restarts the server on the new port. |
| Embot (`Embotenable`) | Toggle | On | Show the Embot builder mascot. He appears on each operator Envoy creates or edits and narrates what it does. Off hides him entirely (no character, no camera movement). |
| Envoy Follow (`Envoyfollow`) | Toggle | On | Camera follows the operator Envoy is working on, panning the network editor to it. Works with or without the Embot character shown. |
| Tool Permissions (`Toolpermissions`) | Menu | `all` | How much Embody pre-approves Envoy MCP tool calls in Claude Code's `.claude/settings.local.json`, so you aren't prompted on every tool use. `Don't ask (all)` auto-approves every Envoy tool (via the `mcp__envoy` wildcard). `Ask for some` auto-approves only read-only/query tools; write tools still prompt. `Ask for all` pre-approves nothing. `Leave settings alone` never creates or modifies the file. Except for "Leave", the OS temp directory is also whitelisted so captured TOP images can be read without a prompt. The setup wizard sets this; changing it here updates the file (merging, preserving your other settings). Claude Code only. |
| Envoy Status (`Envoystatus`) | Str (read-only) | `Running on port 9870` | Current status of the Envoy MCP server (read-only). Shows Running, Stopped, or error information. |

## Logs

| Parameter | Type | Default | Description |
|---|---|---|---|
| Verbose (Debug) (`Verbose`) | Toggle | - | Enables debug-level logging. When off, only INFO and above are logged. Enable for detailed diagnostics when troubleshooting issues. |
| Print to Textport (`Print`) | Toggle | On | Echoes all log messages to the TouchDesigner textport. Useful for monitoring Embody activity in real-time. |
| Log to File (`Logtofile`) | Toggle | On | Writes log messages to a file on disk. Files are saved to the Log Folder with automatic rotation at 10 MB. |
| Log Folder (`Logfolder`) | Folder | `logs` | Folder where log files are saved. Defaults to dev/logs/ in the project directory. Files are named <project>_YYMMDD.log. |

## UI

| Parameter | Type | Default | Description |
|---|---|---|---|
| Enable Keyboard Shortcuts (`Enablekeyboardshortcuts`) | Toggle | On | Enables/disables all Embody keyboard shortcuts. Turn off if shortcuts conflict with other tools. |
| Add Externalization Tag (`Addtagshort`) | Str (read-only) | `lctrl-lctrl (lclick op & press 2x)` | Keyboard shortcut for adding an externalization tag to the selected operator(s). Default: double-press Left Ctrl. |
| Open Manager (`Openmanagershort`) | Str (read-only) | `ctrl/cmd + lshift + o` | Keyboard shortcut for opening the Embody Manager window. |
| Initialize/Update All (`Initializeupdateshort`) | Str (read-only) | `ctrl/cmd + lshift + u` | Keyboard shortcut for triggering Initialize/Update All (Ctrl+Shift+U). Saves all dirty externalized operators to disk. |
| Update Current COMP (`Saveshort`) | Str (read-only) | `ctrl/cmd + alt + u` | Keyboard shortcut for updating/saving the current COMP to its externalized file. |
| Export Project to TDN (`Exportprojecttdn`) | Str (read-only) | `ctrl/cmd + lshift + e` | Export the entire project network to a single .tdn file at your project root. |
| Export Current COMP to TDN (`Exportcomptdn`) | Str (read-only) | `ctrl/cmd + alt + e` | Export the current COMP network to a .tdn file. |
| Text Color (`Textcolor`) | RGBA | - | Text color used throughout the Embody UI (toolbar, manager, dialogs). |
| Background Color (`Backgroundcolor`) | RGBA | - | Background color for the Embody UI panels and windows. |
| Button Background Color (`Buttonbackgroundcolor`) | RGBA | - | Background color for buttons in the Embody UI. |
| List Header Color (`Listheadercolor`) | RGBA | - | Header row color in the Manager list view. |
| List Row Color (`Listrowcolor`) | RGBA | - | Default row color in the Manager list view. |
| List Row Select Color (`Listrowselectcolor`) | RGBA | - | Selected row highlight color in the Manager list view. |
| Saved Color (`Savedcolor`) | RGBA | - | Color indicator for operators that are saved and up-to-date (not dirty). |
| Dirty Color (`Dirtycolor`) | RGBA | - | Color indicator for operators that have unsaved changes (dirty state). |
| Dirty Par Color (`Dirtyparcolor`) | RGBA | - | Color indicator for parameters that have been modified since last save. |
| Uncommitted Color (`Uncommittedcolor`) | RGBA | `0.6` | Color indicator for externalized files that are saved to disk but not yet committed to git (uncommitted). Distinct from the red unsaved color and the amber par-change color. |
| Tagging Menu Color (`Taggingmenucolor`) | RGBA | - | Background color for the tagging menu popup. |
| Highlight Color (`Highlightcolor`) | RGBA | - | Accent/highlight color used for interactive elements in the Embody UI. |
| TDN Saved Color (`Tdnsavedcolor`) | RGBA | - | Color indicator for TDN-strategy operators that are saved and up-to-date. |

## About

| Parameter | Type | Default | Description |
|---|---|---|---|
| `Version` | Str (read-only) | `6.0.65` | Embody version string (read-only). |
| Touch Build (`Touchbuild`) | Str (read-only) | `2025.32820` | TouchDesigner build number this version was developed on (read-only). |
| `Author` | Str (read-only) | `Dylan Roscover` | Embody author (read-only). |
| Build Number (`Build`) | Int (read-only) | `662` | Embody build number (read-only). Incremented with each release. |
| GitHub (`Github`) | Pulse | - | Opens the Embody GitHub repository in your web browser. |
| Build Date (`Date`) | Str (read-only) | `2026-07-01 02:27:32 UTC` | Date of this Embody build (read-only). |
| `Help` | Pulse | - | Opens the Embody help documentation. |
