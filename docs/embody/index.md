# Embody

**Embody** is a TouchDesigner extension that automates externalization of COMP and DAT operators to version-control-friendly files. It solves the fundamental problem of TouchDesigner's binary `.toe` files being impossible to diff or merge in git.

## How It Works

1. **Tag** any operator with a double-tap of ++lctrl++
2. **Update** with ++ctrl+shift+u++ — Embody writes tagged operators to external files
3. **Diff and merge** those files in git like any other source code

Embody maintains a bidirectional sync between your `.toe` project and external files:

- **Sync out**: Press ++ctrl+shift+u++ to update all dirty COMPs and DATs to external files (`.tox` for COMPs, `.py`/`.json`/etc. for DATs)
- **Sync in**: On project open, Embody automatically restores all externalized operators from disk — TOX-strategy COMPs from `.tox` files, TDN-strategy COMPs from `.tdn` JSON files, and DATs via TouchDesigner's native file parameter
- **Tracking**: An `externalizations.tsv` table tracks all externalized ops with path, type, timestamp, dirty state, and build number

## Features

- **Automatic restoration on project open** — all externalized operators are restored from their files on disk, so your `.toe` never needs to be the source of truth
- **Parameter change detection** — tracks all parameter values and marks COMPs dirty when anything changes
- **Build tracking** — adds Build Number, Touch Build, and Build Date to every externalized COMP
- **Safe file management** — only deletes files it created, never touches untracked files
- **Cross-platform** — all paths normalized to forward slashes for Windows/macOS collaboration
- **Duplicate detection** — prompts when two operators point to the same external file
- **Full project externalization** — externalize everything in one click
- **UTC timestamps** — synchronized for international workflows
