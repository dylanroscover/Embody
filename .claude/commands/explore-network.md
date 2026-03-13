Explore the current TouchDesigner network and report its structure:

1. Load `/mcp-tools-reference` if this is the first MCP call
2. Find the current network: `execute_python` with `result = ui.panes.current.owner.path`
3. If $ARGUMENTS contains a path, use that instead of the current network
4. Run `query_network` on the target path to list all operators
5. Run `get_network_layout` to understand spatial organization
6. Run `get_annotations` to see logical groupings
7. Report a clear summary: operator list organized by annotation groups, signal flow direction, and any errors found
