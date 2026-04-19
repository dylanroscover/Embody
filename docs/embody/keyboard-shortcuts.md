# Keyboard Shortcuts

## Quick Reference

| Shortcut | Action |
|----------|--------|
| ++lctrl+lctrl++ | Tag and externalize the selected operator (press left control twice) |
| ++ctrl+shift+u++ | Update all dirty externalizations |
| ++ctrl+alt+u++ | Update only the current COMP you're working inside |
| ++ctrl+shift+r++ | Refresh tracking state |
| ++ctrl+shift+o++ | Open the Manager UI |
| ++ctrl+shift+e++ | Export entire project network to `.tdn` file |
| ++ctrl+alt+e++ | Export current COMP network to `.tdn` file |

## Tagging

The double-tap ++lctrl++ shortcut works on the currently selected operator in the network editor. It tags the operator and externalizes it immediately:

- A visual tag appears on the operator and it is saved to disk in one step
- Pressing ++lctrl+lctrl++ again on a tagged operator removes the tag

## Update Operations

- **++ctrl+shift+u++** — Updates all dirty externalized operators. If Embody hasn't been initialized yet, this also performs first-time setup. This is the primary way to update your externalizations.
- **++ctrl+alt+u++** — Updates only the COMP you're currently inside. Useful for large projects where a full update takes too long.
- **++ctrl+shift+r++** — Refreshes tracking state, re-scanning externalized operators for changes without writing files.

!!! tip "Your externalized files are the source of truth"
    You don't need to ++ctrl+s++ to preserve externalized work. Use ++ctrl+shift+u++ instead — it writes all dirty operators to files on disk. On project open, Embody restores everything automatically: TOX-strategy COMPs from `.tox` files and TDN-strategy COMPs from `.tdn` files.

!!! warning "Ctrl+S strips TDN containers"
    When you do save the `.toe` with ++ctrl+s++, Embody **temporarily strips all children** from TDN-strategy COMPs to keep the `.toe` file small. They are restored immediately after the save completes — but if TD crashes during the save, those children will be missing from the `.toe`. This is fine: they'll be reconstructed from `.tdn` files the next time the project opens. Just make sure you've updated your externalizations with ++ctrl+shift+u++ first.

## TDN Export

- **++ctrl+shift+e++** — Exports the entire project network to a single `.tdn` file at your project root
- **++ctrl+alt+e++** — Exports only the current COMP's network to a `.tdn` file

!!! info "Export vs Update — what's the difference?"
    The **Update** shortcuts (++ctrl+shift+u++ / ++ctrl+alt+u++) update operators that are **already tagged and tracked** by Embody. They write files, increment build numbers, and clear dirty state — this is your daily workflow.

    The **Export** shortcuts (++ctrl+shift+e++ / ++ctrl+alt+e++) create a standalone `.tdn` snapshot of **any** network, whether or not it's externalized. No tracking, no build increment, no side effects. Use these when you want to grab a snapshot of a network you haven't tagged — like exporting someone else's component, creating a one-off backup, or sharing a network as a `.tdn` file.
