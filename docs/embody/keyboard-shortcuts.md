# Keyboard Shortcuts

## Quick Reference

| Shortcut | Action |
|----------|--------|
| ++lctrl+lctrl++ | Open the tagger UI for the hovered operator (press left control twice) |
| ++ctrl+shift+u++ | Update all dirty externalizations |
| ++ctrl+alt+u++ | Update only the current COMP you're working inside |
| ++ctrl+shift+r++ | Refresh tracking state |
| ++ctrl+shift+o++ | Open the Manager UI |
| ++ctrl+shift+e++ | Export entire project network to `.tdn` file |
| ++ctrl+alt+e++ | Export current COMP network to `.tdn` file |
| ++ctrl+shift+c++ | Copy the selected COMP to the clipboard as a portable TDN envelope |

## Tagging

The double-tap ++lctrl++ shortcut works on the operator your cursor is hovering over in the network editor (not the current selection). It opens the tagger UI for that operator:

- On an **untagged** operator, the tagger lets you pick how to externalize it — a strategy (TOX or TDN) for a COMP, or a file format for a DAT
- On an **already-tagged** operator, the tagger lets you switch strategy, remove the tag, or save the externalization

## Update Operations

- **++ctrl+shift+u++** — Updates all dirty externalized operators. If Embody hasn't been initialized yet, this also performs first-time setup. This is the primary way to update your externalizations.
- **++ctrl+alt+u++** — Updates only the COMP you're currently inside. Useful for large projects where a full update takes too long.
- **++ctrl+shift+r++** — Refreshes tracking state, re-scanning externalized operators for changes without writing files.

!!! tip "Keep your externalizations up to date"
    Use ++ctrl+shift+u++ to write all dirty operators to files on disk. On project open, TOX-strategy COMPs are always restored from `.tox` files and DATs sync from their externalized source files (`.py`, `.txt`, `.json`, ...). TDN-strategy COMPs are reconstructed from `.tdn` files **only in Roundtrip mode** — in the default Export-on-Save mode the `.toe` stays authoritative and is not rebuilt from `.tdn` on open. See [TDN Mode](externalization.md#tdn-mode-master-switch).

!!! warning "Roundtrip mode strips TDN containers on Ctrl+S"
    In **Roundtrip mode** (and only when strip-on-save is enabled), saving the `.toe` with ++ctrl+s++ makes Embody **temporarily strip all children** from TDN-strategy COMPs to keep the `.toe` file small. They are restored immediately after the save completes — but if TD crashes during the save, those children will be missing from the `.toe`. This is fine: they'll be reconstructed from `.tdn` files the next time the project opens. Just make sure you've updated your externalizations with ++ctrl+shift+u++ first. In the default **Export-on-Save mode**, Ctrl+S does **not** strip: the `.toe` keeps its children and remains the source of truth.

## TDN Export

- **++ctrl+shift+e++** — Exports the entire project network to a single `.tdn` file at your project root
- **++ctrl+alt+e++** — Exports only the current COMP's network to a `.tdn` file

!!! info "Export vs Update — what's the difference?"
    The **Update** shortcuts (++ctrl+shift+u++ / ++ctrl+alt+u++) update operators that are **already tagged and tracked** by Embody. They write files, increment build numbers, and clear dirty state — this is your daily workflow.

    The **Export** shortcuts (++ctrl+shift+e++ / ++ctrl+alt+e++) create a standalone `.tdn` snapshot of **any** network, whether or not it's externalized. No tracking, no build increment, no side effects. Use these when you want to grab a snapshot of a network you haven't tagged — like exporting someone else's component, creating a one-off backup, or sharing a network as a `.tdn` file.

## Clipboard (TDN copy / paste)

- **++ctrl+shift+c++** — Copy the selected COMP to the OS clipboard as a portable TDN envelope. Paste it into another Embody project, or share it; the web Collection's "embody it" button writes the same envelope.
- **There is no paste shortcut — pasting is automatic.** With **Clipboard Auto-Paste** on (default), Embody watches the clipboard and, when an **inbound** TDN network appears (e.g. the web "embody it" button), prompts to paste it as a new COMP in the current network. Your own ++ctrl+shift+c++ **outbound** copy is recognized and does **not** trigger the prompt — you copied it to share or paste elsewhere, not to re-import it. The old ++ctrl+shift+v++ binding was removed: TouchDesigner's native operator-paste fires on the same key, so it pasted stray nodes alongside the TDN. See [Clipboard Auto-Paste](configuration.md).
