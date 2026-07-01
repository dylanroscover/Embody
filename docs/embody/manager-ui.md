# Manager UI

Press ++ctrl+shift+o++ to open the Embody Manager window.

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

Type **`changed`** in the filter box to show only rows with pending changes on *either* axis -- unsaved (red/amber) **or** git-uncommitted (orange).

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
| Import TDN | Import a `.tdn` file | — |
| Export COMP | Export current COMP to `.tdn` | ++ctrl+alt+e++ |
| Export Project | Export entire project to `.tdn` | ++ctrl+shift+e++ |
| Envoy | Toggle Envoy MCP server | — |
| Pars | Open the Embody COMP's parameter dialog | — |
| Filter | Filter box — type to narrow the list (e.g. `changed`); clear it to show all rows | — |
| Close | Close the Manager window | — |

The toolbar is also visible in minimized mode with a compact subset of buttons.

## Quick Actions

- **Click** to navigate to any operator in the network editor
- **Open file location** in your system file browser
- **Export portable tox** to save a self-contained `.tox` with no external dependencies
- **Filter/search** through externalized operators
