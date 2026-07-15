# Keyboard Shortcuts

## Quick Reference (defaults)

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

On macOS the factory defaults use **Cmd** (`cmd+shift+o`, `cmd+alt+u`, …) —
the platform's idiomatic app-shortcut modifier. `ctrl` and `cmd` name
*distinct physical keys*: macOS keyboards have both (so `ctrl+shift+o` and
`cmd+shift+o` are two different bindings there), while PC keyboards have
only Ctrl — a Mac-authored `cmd+...` binding automatically fires via Ctrl
on Windows/Linux. Saved values are never rewritten when a project changes
platforms, so bindings round-trip intact.

**Every shortcut is editable.** If a default collides with your own workflow
hotkeys, remap or disable it on the Embody COMP's **Shortcuts** parameter
page — see [Customizing Shortcuts](#customizing-shortcuts).

## Customizing Shortcuts

Open the Embody COMP's parameter dialog and switch to the **Shortcuts** page.
Each action has an editable binding and a **Record** pulse:

- **Type a combo** directly into the binding parameter — lowercase, joined
  with `+`: modifiers (`ctrl`, `cmd`, `alt`, `shift`) plus one trigger key,
  e.g. `ctrl+shift+o`, `cmd+alt+e`, or `alt+F5`. `ctrl` and `cmd` are
  distinct physical keys on macOS (a combo may even use both, e.g.
  `ctrl+cmd+k`); on Windows/Linux `cmd` bindings fire via Ctrl. Invalid
  input reverts with a warning; anything you type is normalized to the
  canonical form (`CMD + SHIFT + O` becomes `cmd+shift+o`). Trigger keys
  can be letters, digits, most punctuation, `F1`–`F12`, or named keys like
  `space`, `tab`, `enter`, `pageup`, `printscreen`. The characters `+`,
  `-`, `.` and space act as separators, so they can't themselves be bound —
  nor can ++esc++ (it cancels the recorder) or a modifier on its own.
- **Or record one**: pulse **Record**, then press the keys you want. Held
  modifiers preview in the status bar and never commit on their own — the
  first *non-modifier* key commits the combo with whatever modifiers are
  held at that instant. Press ++esc++ to cancel; an armed recorder times
  out after 10 seconds if nothing is pressed.
- **Disable a shortcut** by clearing its binding (empty = off). The
  **Enable Keyboard Shortcuts** toggle at the top of the page switches all
  of them (including the tagger double-tap) at once.
- **Tagger Double-Tap Key** is a menu, not a combo — pick which physical
  modifier key opens the tagger when double-tapped, or turn it off. The
  choices adapt to the platform's keyboards: macOS offers Left Ctrl and
  left/right **Cmd** (Apple keyboards have no right Ctrl key) plus Alt and
  Shift; Windows/Linux offers left/right **Ctrl** (no Cmd key exists) plus
  Alt and Shift. A choice the other platform's keyboard lacks falls back to
  its closest key there (Cmd → Ctrl on Windows/Linux, Right Ctrl → Left
  Ctrl on macOS) — the saved value is never rewritten, so it round-trips
  between platforms intact.
- **Reset Shortcuts to Defaults** restores the factory bindings above.

Custom bindings are saved to `.embody/config.json` with the rest of your
settings, so they survive Embody upgrades.

### Conflicts

- **Duplicates are blocked.** A combo may drive exactly one action: if,
  while recording, you press a combo that another action already holds, a
  dialog explains which action owns it and recording continues (with a
  fresh timeout) so you can press a different combo — or ++esc++ to cancel.
  Typing a duplicate into the parameter reverts with a warning. To swap
  two bindings, clear one first.
- **TD built-ins warn.** Assigning one of TouchDesigner's own shortcuts
  (read live from `TouchShortcuts.txt`, including your user overrides)
  logs a warning and shows it in the status bar.

!!! warning "TouchDesigner built-ins cannot be suppressed"
    Embody's Keyboard In DAT observes keys — it cannot block them. If you
    bind an Embody action to a TD built-in like ++ctrl+z++, **both** will
    fire. Prefer `ctrl+shift+...` / `ctrl+alt+...` combos that TD leaves
    free, and heed the warning when one is reserved. Note that OS-level
    shortcuts (macOS system hotkeys especially) are intercepted before TD
    ever sees them and cannot be recorded or triggered at all.

## Tagging

The double-tap ++lctrl++ shortcut (configurable — see above) works on the
operator your cursor is hovering over in the network editor (not the current
selection). It opens the tagger UI for that operator:

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
