# TouchDesigner UI / Panel Building

Rules for building UI inside TouchDesigner (dialogs, wizards, panels, HUDs,
control surfaces). Verify any TD claim here against the Derivative docs before
relying on it (see `td-python.md` -> Verify Before Claiming).

## Use Panel COMPs for UI -- NEVER a Text TOP for UI text

**Text in a UI panel is a Text COMP, not a Text TOP.** This is the single most
common UI mistake.

- **Text COMP** (COMP family) is a *panel widget* purpose-built to show text in a
  panel. It has panel geometry (`w`/`h`, size modes) plus `text`, `fontsizex`,
  `textcolor*`, `alignx`/`aligny`, `wordwrap`. It renders at panel scale
  directly -- correct size, no resolution juggling.
- **Text TOP** (TOP family) renders text to a *texture*. It has `resolutionw/h`
  (not `w`/`h`) and is meant for compositing text into TOP/render chains. Used as
  a panel background it renders at a default resolution and gets **scaled up into
  giant, blurry text** -- exactly the failure to avoid.

Rule: for any label, title, message, or button caption, use a **Text COMP**.
Reserve Text TOP for image/render pipelines, never as a UI element.

## The Panel COMP toolbox

| Need | Panel COMP |
|---|---|
| Layout / grouping / a dialog body | **Container COMP** (set `align` = `verttb`/`horizlr` to auto-stack children) |
| Text / label / title / message | **Text COMP** |
| Clickable action | **Button COMP** (its caption is a **Text COMP child**, not a built-in label -- a bare Button shows the placeholder "Button") |
| Text input | **Field COMP** |
| Numeric drag / range | **Slider COMP** |
| Rows / choices | **List COMP** or **Table COMP** |
| Expose a parameter | **Parameter COMP** |

## Match the project's existing UI -- clone, don't reinvent

Before hand-building widgets, look at how the project already builds its UI and
**clone a working widget** (e.g. Embody's `tagger` buttons/header). Cloning
inherits the correct COMP type, styling, caption mechanism, and callbacks, so you
change only the caption text + the callback -- far more reliable than
reconstructing panel params from scratch and guessing.

## Window COMPs: ONE for the main UI; separate windows OK for dialogs

**For a project's MAIN UI, prefer a SINGLE Window COMP** -- don't spin up a
window per screen of the primary interface. The pattern:

1. Build each main-UI surface (HUD, control panel, page) as a **Container**.
2. Collect them under **one main container** that reveals/swaps the active view
   (and holds per-display content when driving secondary screens).
3. Point **one Window COMP** at that main container via `winop`. New main-UI
   screens are **VIEWS inside the main container**, not new windows.

**Main UI and visual output live in the SAME container + window.** When a project
has both a control UI and rendered visual output (content on a screen /
projector / secondary display), do NOT split them across separate windows.
Compose both inside the ONE main container driven by the ONE Window COMP — the
visual output as the background/base layer, the UI as an overlay panel on top,
and secondary displays as additional views/regions of that same main container.
One window renders everything.

**Dialogs are exempt.** A transient / modal dialog -- a wizard, a confirmation, a
tool palette (e.g. Embody's own tagger and manager) -- may use its own Window
COMP. Dialogs are short-lived and independent of the main UI, so a dedicated
window is appropriate.

Rule of thumb: **main app UI = one window + view-switching; a dialog = its own
window is fine.**

## Showing / switching a panel via the Window COMP

- The **Window COMP** displays a panel COMP as an OS window: `winop` = the main
  container, `winw`/`winh` size, `justifyh`/`justifyv` placement.
- **`winopen` is a Pulse**: `win.par.winopen.pulse()` to open; `win.isOpen` to
  check; `win.par.winclose.pulse()` to close.
- A Window COMP is modeless -- it does NOT block the main thread (unlike
  `ui.messageBox`). To show a different screen, **switch the main container's
  active view** (e.g. a Container/Switch that selects the child to display) --
  never open a second window.

## Handling clicks

Use a **Panel Execute DAT** (class `panelexecuteDAT`): set its `panel` to the
button COMP(s) to watch, enable the `Off to On` callback (`par.offtoon`), and
implement `onOffToOn(panelValue)` -- `panelValue.owner` is the clicked COMP.

## When to reach for a custom panel vs `ui.messageBox`

- **`ui.messageBox`** is correct for simple dialogs: a short message + 1-4
  buttons. Keep using it for those -- it is the least code.
- **A custom panel/wizard** is for anything more complex: multi-step flows, lists
  of items, forms, or content that would overflow a message box. Prefer a
  step-at-a-time wizard (one decision per screen) over one dense scrolling panel.

## Building UI via code -- gotchas that each cost a rebuild

Creating panel COMPs via `execute_python`/`.create()` follows the same layout
discipline as any op creation (see `network-layout.md`) -- position child nodes
tidily, never leave them at (0, 0). Beyond that, the TD-specific traps:

- **`execute_python` is transactional.** A mid-script exception ROLLS BACK every
  op the script created -- so a buggy trailing `result`/diagnostic line silently
  undoes all your real work. Keep result/log lines bulletproof (no `sorted()`
  over mixed `None`, no unguarded attribute access).
- **`.destroy()` is deferred.** Destroy-then-recreate with the same name in one
  script collides (new op gets a numeric suffix). Tear down in one call, rebuild
  in the next -- and TD may still briefly reserve a just-destroyed name, so write
  logic that tolerates suffixes (match by substring / iterate children), never
  hardcode exact child names.
- **A fresh `buttonCOMP` gets its child ops (`text`, `out1`, `panelexecN`) on the
  NEXT frame**, not synchronously. Its caption is that built-in `text` **child**
  (a Text COMP), NOT a `text` par -- set labels in a follow-up call; reading them
  same-frame gives `None`.
- **Button behavior is `buttontype`** (`momentary`, `toggledown`, ...), not `type`.
- **Text padding is `textoffsetx`/`textoffsety`.** A Text COMP ignores `marginl`
  for its rendered text (margins inset *containers*, not text).
- **`verttb`/`horizlr` stacking order is the child `order` par, not creation
  order.** Omit `order` and the title can stack at the BOTTOM.
- **Panel sizing is `hmode`/`vmode`** (`fixed`/`fill`/`anchors`) + `hfillweight`/
  `vfillweight`. A child with `vmode='fill'` is a flex spacer -- use one to push a
  footer to the bottom.
- **`sizefromwindow=True` sizes the panel to whatever OP displays it** -- including
  a debug OP Viewer's resolution (e.g. 1280x720), which silently blows the panel
  up and crops it. For a fixed dialog keep it OFF, set `w`/`h`, and size the
  *window* (`winw`/`winh`) with headroom for the title bar.
- **`winopen` is a Pulse** (`win.par.winopen.pulse()`); `winclose.pulse()` closes.

## Radio selection: native latch, not render-driven color

A render-driven "set the selected button's `bgcolor`" radio is fragile -- it
needs the callback to fire on every real click, and a latched button's on-color
overrides your `bgcolor` anyway. Instead: make the options **`toggledown`**
buttons whose ON color (`colorr/g/b`) IS the selected look -- the native latch
then shows a persistent selection on its own, independent of your render. Enforce
single-select in the Panel Execute callback by zeroing the OTHER options'
`value0`; read the current choice from whichever button's `value0` is on.

## Verify panel UI with a temporary OP Viewer TOP -- you CAN see it

`capture_top` does NOT work on a panel COMP directly -- but an **OP Viewer TOP**
aimed at the panel renders it to a TOP you CAN `capture_top`. Create one, point
its operator-viewer reference at the UI (verify the exact par name in your build),
and capture after EVERY change -- iterate against what you SEE. Never build panel
UI blind from the user's screenshots; that is what turns a 20-minute job into an
hour of thrash. Delete the temporary OP Viewer once the UI is approved.
