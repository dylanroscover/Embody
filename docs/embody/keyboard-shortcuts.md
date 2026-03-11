# Keyboard Shortcuts

## Quick Reference

| Shortcut | Action |
|----------|--------|
| ++lctrl+lctrl++ | Tag and externalize the selected operator (press left control twice) |
| ++ctrl+shift+u++ | Save/update all dirty externalizations |
| ++ctrl+alt+u++ | Save only the current COMP you're working inside |
| ++ctrl+shift+o++ | Open the Manager UI |
| ++ctrl+shift+e++ | Export entire project network to `.tdn` file |
| ++ctrl+alt+e++ | Export current COMP network to `.tdn` file |

## Tagging

The double-tap ++lctrl++ shortcut works on the currently selected operator in the network editor. It tags the operator and externalizes it immediately:

- A visual tag appears on the operator and it is saved to disk in one step
- Pressing ++lctrl+lctrl++ again on a tagged operator removes the tag

## Save Operations

- **++ctrl+shift+u++** — Saves all dirty externalized operators as you work. If Embody hasn't been initialized yet, this also performs first-time setup. This is the primary way to save your externalizations.
- **++ctrl+alt+u++** — Saves only the COMP you're currently inside. Useful for large projects where a full save takes too long.

!!! tip "Your externalized files are the source of truth"
    You don't need to ++ctrl+s++ to preserve externalized work. Use ++ctrl+shift+u++ instead — it writes all dirty operators to files on disk. On project open, Embody restores everything automatically: TOX-strategy COMPs from `.tox` files and TDN-strategy COMPs from `.tdn` files.

!!! warning "Ctrl+S strips TDN containers"
    When you do save the `.toe` with ++ctrl+s++, Embody **temporarily strips all children** from TDN-strategy COMPs to keep the `.toe` file small. They are restored immediately after the save completes — but if TD crashes during the save, those children will be missing from the `.toe`. This is fine: they'll be reconstructed from `.tdn` files the next time the project opens. Just make sure you've saved your externalizations with ++ctrl+shift+u++ first.

## TDN Export

- **++ctrl+shift+e++** — Exports the entire project network to a single `.tdn` file at your project root
- **++ctrl+alt+e++** — Exports only the current COMP's network to a `.tdn` file
