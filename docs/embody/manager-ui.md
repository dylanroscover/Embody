# Manager UI

Press ++ctrl+shift+o++ (default — remappable on the [Shortcuts page](keyboard-shortcuts.md#customizing-shortcuts)) to open the Embody Manager window.

## Features

- **Tree View**: Hierarchical view of all externalized operators organized by path
- **Status Indicators**: Two independent status axes per operator (see [Status Indicators](#status-indicators) below) -- unsaved-vs-disk (red/amber) and git-uncommitted (orange)
- **Build Information**: Displays build number, TouchDesigner build, and timestamp for each externalized COMP

## Status Indicators

The manager shows **two independent status axes** for each externalized operator.

### Unsaved changes (red / amber)

- **Red** -- the operator was modified in memory but not yet written to disk. Press ++ctrl+shift+u++ (Update All) or ++ctrl+alt+u++ (Update Current) to externalize it.
- **Amber ("Par")** -- only parameter *values* changed (no network-structure edit). Marked distinctly so a pure parameter tweak is easy to spot.

### Git-uncommitted (orange)

- **Orange** -- the externalized file is saved to disk but **not yet committed to git**. This is a separate axis from the red "unsaved" state: a file can be clean-on-disk yet still show orange because the change has not been committed.
- Computed by an async `git status --porcelain` scan (it runs off the refresh thread, so there is no frame drop) that maps changed files back to operator paths. Self-disables outside a git repository.
- The badge color is the `Uncommittedcolor` parameter (see [Configuration](configuration.md)).
- After a `git commit`, trigger a manager **Refresh** (++ctrl+shift+r++) so the orange badges clear.

### Filter by changes

Two reserved keywords narrow the list by status instead of by name:

- Type **`changed`** in the filter box to show only rows with pending changes on *either* axis -- unsaved (red/amber) **or** git-uncommitted (orange).
- Type **`dirty`** to show only rows with unsaved in-TD changes (red/amber) -- ignoring git state.

While any filter is active, matching rows are revealed even if their branch was collapsed; clearing the filter restores your expand/collapse state.

## Toolbar

The toolbar provides quick access to common operations. All buttons with keyboard shortcuts show the shortcut in their tooltip.

| Button | Action | Shortcut |
|--------|--------|----------|
| Toggle | Enable/disable externalization | — |
| Refresh | Refresh tracking state | ++ctrl+shift+r++ |
| Update All | Update all dirty externalizations | ++ctrl+shift+u++ |
| Update Current | Update only the current COMP | ++ctrl+alt+u++ |
| Perform | Toggle Perform Mode (suspends Embody compute) | — |
| Save Folder | Open the externalization folder | — |
| Import TDXN | Import a `.tdxn` file | — |
| Export COMP | Export current COMP to `.tdxn` | ++ctrl+alt+e++ |
| Export Project | Export entire project to `.tdxn` | ++ctrl+shift+e++ |
| Envoy | Toggle Envoy MCP server | — |
| Pars | Open the Embody COMP's parameter dialog | — |
| Filter | Filter box — type to narrow the list (keywords: `changed`, `dirty`); clear it to show all rows | — |
| Clear filter (X) | Empties the filter box beside it — it does *not* close the window | — |

To close the Manager window, use the close button in its title bar or pulse **Close Manager** on the Embody COMP.

The toolbar is also visible in minimized mode with a compact subset of buttons.

## Quick Actions

- **Click** to navigate to any operator in the network editor
- **Open file location** in your system file browser
- **Export portable tox** to save a self-contained `.tox` with no external dependencies — honors `pre_release`/`post_release` hook DATs in the exported COMP (see [Script hooks](externalization.md#script-hooks))
- **Filter/search** through externalized operators

### Row actions

Clicking a row's **Strategy** cell opens that operator's **Actions** menu. On an
untagged COMP it offers the strategy choice (TOX or TDXN); on an already-tagged
COMP it offers:

Since v6.0.278 the menu is ordered so the most-used actions (**Save**/**Reload**) sit at the center, where the cursor lands, with destructive entries pushed to the bottom edge:

| Action | What it does |
|--------|--------------|
| **Reveal in Finder** / **Reveal in Explorer** | Opens the externalized file's folder. Shown only when the operator has a file on disk. |
| **Embed DATs in tdn** | TDXN only. Per-COMP toggle (a check mark means on) overriding the **Embed DATs (default)** parameter. |
| **Embed storage in tdn** | TDXN only. Per-COMP toggle for storage capture, same override behavior. |
| **Copy tdn** / **Paste tdn** | TDXN only. Copies the COMP to the clipboard as a portable TDXN envelope, or pastes one over it. |
| **Save tox** / **Save tdn** | Writes the externalization now. This is the one *explicit* save gesture, so it also overrides the empty-network overwrite guard — a deliberately emptied COMP can be written over its file here, where the automatic exports refuse (see [Externalization](externalization.md)). |
| **Reload tox** / **Reload tdn** | Re-imports the COMP from its file on disk, discarding in-TD changes. Since v6.0.241 a `.tdxn` reload also rebuilds nested externalized children from their own `.tdxn` files, so nothing is left as an empty shell. |
| **Export portable tox** | Writes a self-contained `.tox` with no external dependencies. |
| **Convert to tox** / **Convert to tdn** | Switches the strategy; the button reads **Remove tox** / **Remove tdn** for the strategy that is currently active, which untags the operator instead. |
| **Exclude from tdn** | TDXN only, since v6.0.278. Opens the exclusion panel scoped to this COMP: drag a parameter onto its drop zone to keep that value out of `.tdxn` exports (`tdn_exclude:<parname>`), or a COMP to exclude it entirely (bare `tdn_exclude`); every exclusion in the subtree is listed with a per-row **×**. See [Externalization](externalization.md#excluding-a-parameters-value-the-tdn_excludepar-tag). |

Clicking a row's **File** cell opens the externalized file itself.

## Status readout (the Embody node viewer)

The Embody COMP's own node viewer carries a live status readout -- the
at-a-glance surface you get without opening anything. It is three rows, and the
**marks are the readout**:

```
[mark] Embody      v6.0.241
[mark] Saved 2m ago
[mark] Envoy  [mark] Convoy
```

Words that a mark already says ("Enabled", "Connected", the port number) are
deliberately absent, which is what pays for the larger type at node-tile size.

### What the marks mean

| Mark | State |
|------|-------|
| `✓` | Fine -- running, connected, done |
| `✗` | Failed. The reason is in that subsystem's status parameter and the log |
| `!` | Waiting on you -- an update is available, or an enabled Convoy has no host app |
| `-` | Deliberately off or skipped (Envoy disabled, auto-save bypassed in Perform Mode) |
| Spinner (a rotating bar, slash, dash, backslash) | Work in flight -- installing, updating, registering |
| (blank) | Nothing claimed yet this session |

A spinning mark that turns **red** means *working too long*, not failed: the step
is still running but has passed its dwell. The dwell is per operation -- 20
seconds for routine work such as a restart, but ten minutes for the legitimately
slow ones (environment builds, dependency installs, downloads, repairs, and the
host app's start), so a first-run dependency install no longer reddens on its way
to succeeding.

### The rows

- **Embody + version.** The mark is Embody's own state; the version sits at the
  right, grey at rest. It turns **red only when an update is waiting or failed**,
  and spins while one is being checked, downloaded, or installed.
- **Saved.** How long ago your work reached disk -- `12s ago`, `3m ago`,
  `2h ago`, `51d ago`, or `never` on a project that has not checkpointed yet. The
  age is measured against the calendar, so a weeks-old project says so instead of
  folding into a 24-hour clock, and it is seeded at startup from the newest write
  the externalizations table records, so the answer is there before you ask.
- **Envoy and Convoy.** One mark each, side by side.

Two things this panel will not do: it never renders a percentage (nothing here
has an honest denominator, so a busy step shows an elapsed clock instead), and it
never changes shape -- the same three rows during an install, when settled, and
when broken.

Every row is derived from parameters on the Embody COMP (`Status`,
`Autosavestatus`, `Envoystatus`, `Convoystatus`, `Version`, `Updatestatus`,
`Autoupdate`), so the panel cannot drift from what those parameters say. Full
failure detail lives there and in the [log](troubleshooting.md).
