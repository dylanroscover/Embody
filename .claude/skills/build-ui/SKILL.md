# Build UI — visual design system for TouchDesigner panels

Load BEFORE building or restyling any TD panel UI (dialogs, wizards, HUDs,
control surfaces). This is the design layer; `rules/td-ui.md` is the mechanics
(which COMP to use, single-window, etc.). A panel is not done until a captured
frame / live window proves it reads cleanly.

The failure this prevents: font sizes all over the place, giant buttons, no
padding, no alignment, no hierarchy, low contrast — "novice" UI. The cure is a
**small set of tokens applied consistently**. Never pick a size, color, or gap
ad hoc; pull it from the scales below.

## 1. Design tokens (dark theme — TD colors are 0..1 floats)

**Color** — a restrained palette. More colors ≠ better; use opacity/size for
hierarchy, accent only for the primary action.

| Token | RGB (0..1) | Use |
|---|---|---|
| `bg` | 0.11, 0.12, 0.115 | window / panel background |
| `surface` | 0.16, 0.17, 0.165 | cards, secondary buttons, input fields |
| `surface-hi` | 0.21, 0.22, 0.215 | hover / pressed |
| `border` | 0.26, 0.27, 0.265 | dividers, 1px outlines |
| `text` | 0.92, 0.92, 0.92 | primary text |
| `text-muted` | 0.60, 0.61, 0.60 | secondary / hints / captions |
| `accent` | 0.24, 0.52, 0.35 | PRIMARY / recommended action only |
| `accent-text` | 0.96, 0.98, 0.96 | text on accent |

Rule: exactly one accent element per screen (the primary action). Everything else
is `surface`/`text`. Match the host project's existing palette when it has one
(e.g. Embody's tagger dark) instead of inventing.

**Type scale** — 3–4 sizes, never more. `fontsizex` on a Text COMP:

| Role | size | color |
|---|---|---|
| Title / H1 | 20 | `text` |
| Section / H2 | 15 | `text` |
| Body / label | 12 | `text` |
| Caption / hint | 11 | `text-muted` |

If two things are the same role, they are the same size. Establish hierarchy with
size + color, not five random sizes.

**Spacing** — a 4px grid. Every gap, pad, and size is one of these: **4, 8, 12,
16, 24, 32**. No arbitrary values.

| Token | px | Use |
|---|---|---|
| `pad-edge` | 24 | panel inner padding from every edge |
| `gap-section` | 16 | between title / body / actions |
| `gap-item` | 8–12 | between sibling items in a group |
| `btn-h` | 32 | button height (NEVER a full-height band) |
| `btn-pad-x` | 16 | horizontal padding inside a button |

## 2. Layout rules

- **Padding first.** Content never touches the panel edge. Inset all content by
  `pad-edge` (24). In TD: put content in an inner container smaller than the
  outer by 2×pad on each axis, or use align margins — the outer `bg` showing
  around it IS the padding.
- **Left-align body text and lists.** Center ONLY short titles and button
  captions. Never center a multi-line sentence — it looks broken.
- **Constrain width + wrap.** A Text COMP must have a width and `wordwrap` on so
  text wraps inside the panel instead of running off the edge. Long line length
  hurts readability — keep body under ~60 chars per line.
- **Align to a single left edge.** Title, body, and controls share one left
  margin. Ragged left edges read as broken.
- **Buttons are modest.** `btn-h` = 32, width = content + 2×`btn-pad-x`, or equal
  columns with a `gap-item` between. A button must NEVER fill a whole row's
  height — that's the #1 tell of novice TD UI.
- **Explicit vertical order.** With a `verttb` container, children stack in panel
  order; confirm the title ends up on TOP, not the bottom. Verify, don't assume.

## 3. Hierarchy, contrast, rhythm

- **Hierarchy:** one clear focal point (the title or the primary action), then
  supporting text, then hints. Drive it with size + color, not boxes everywhere.
- **Contrast:** `text` on `bg` for anything readable; `text-muted` only for
  genuinely secondary info. The primary/recommended button uses `accent`; the
  alternative uses `surface`. This makes the recommended path obvious at a glance.
- **Rhythm:** consistent gaps. Equal spacing between peers; a larger gap
  (`gap-section`) separates groups. Inconsistent spacing looks accidental.
- **Restraint:** flat surfaces, one accent, generous space. Do not add borders,
  gradients, or colors "to fill space" — space IS the design.

## 4. Component recipes

- **Dialog / wizard step:** outer `bg` container → inner content container inset
  by `pad-edge` → [Title 20] · `gap-section` · [Body 12, left-aligned, wrapped] ·
  `gap-section` · [action row]. Action row = right-aligned buttons, `gap-item`
  apart, `btn-h` tall; primary = `accent`, secondary = `surface`.
- **Button:** Button COMP (`surface` or `accent` bg) + a **Text COMP child**
  caption (size 12, centered, `text`/`accent-text`). Height `btn-h`. Never a bare
  Button (shows "Button") and never a Text TOP (see `rules/td-ui.md`).
- **One decision per screen (wizards):** a short title, one question, 2–3 clear
  options; the recommended one is the accent button. Don't crowd a screen.

## 5. Process (do this, don't eyeball it)

1. **Pick tokens up front** — the exact sizes/colors/gaps you'll use, from §1.
2. **Lay out on the grid** — compute positions/sizes from the spacing scale; put
   content inside the `pad-edge` inset.
3. **Clone, don't reinvent** — copy a working widget from the host project
   (Embody's tagger) so styling/caption/callback come for free.
4. **Verify visually with a temporary OP Viewer TOP — never build panel UI
   blind.** You CANNOT `capture_top` a panel COMP directly, but you CAN point a
   temporary **OP Viewer TOP** (`opviewerTOP`, set `.par.op` = the panel COMP)
   at it and `capture_top` THAT to see exactly how the panel renders. Do this
   after EVERY change and CHECK: type scale consistent? one accent? padding on
   every edge? left edges aligned? nothing overflowing or cropped? title on top?
   If you find yourself guessing from the user's screenshots instead of your own
   captures, you have skipped this step — stop and add the OP Viewer.
5. **Iterate against the checklist**, not vibes — re-capture the OP Viewer after
   each tweak until it passes.
6. **Remove the probe when done.** Once the UI is right and the user approves,
   delete the temporary OP Viewer TOP. It is a build-time verification tool, not
   part of the shipped UI.

## Anti-patterns (the exact mistakes to never ship)

- Text running off the panel edge (no width / no `wordwrap`).
- Buttons that fill a whole row's height, or differ in height from each other.
- More than ~4 font sizes; the same role at different sizes.
- Centered multi-line body text; ragged left edges.
- Zero padding — content flush to the window frame.
- No visual difference between the primary and secondary action.
- Using a Text TOP for panel text (renders scaled/blurry — use a Text COMP).
