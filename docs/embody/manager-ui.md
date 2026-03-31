# Manager UI

Press ++ctrl+shift+o++ to open the Embody Manager window.

## Features

- **Tree View**: Hierarchical view of all externalized operators organized by path
- **Status Indicators**: Shows dirty state for each operator (network changes or parameter changes marked as "Par")
- **Build Information**: Displays build number, TouchDesigner build, and timestamp for each externalized COMP

## Toolbar

The toolbar provides quick access to common operations. All buttons with keyboard shortcuts show the shortcut in their tooltip.

| Button | Action | Shortcut |
|--------|--------|----------|
| Toggle | Enable/disable externalization | — |
| Refresh | Refresh tracking state | ++ctrl+shift+r++ |
| Update All | Update all dirty externalizations | ++ctrl+shift+u++ |
| Update Current | Update only the current COMP | ++ctrl+alt+u++ |
| Save Folder | Open the externalization folder | — |
| Import TDN | Import a `.tdn` file | — |
| Export COMP | Export current COMP to `.tdn` | ++ctrl+alt+e++ |
| Export Project | Export entire project to `.tdn` | ++ctrl+shift+e++ |
| Envoy | Toggle Envoy MCP server | — |

The toolbar is also visible in minimized mode with a compact subset of buttons.

## Quick Actions

- **Click** to navigate to any operator in the network editor
- **Open file location** in your system file browser
- **Export portable tox** to save a self-contained `.tox` with no external dependencies
- **Filter/search** through externalized operators
