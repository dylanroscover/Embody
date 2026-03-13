Perform a quick health check of the Embody project:

1. Load `/mcp-tools-reference` if this is the first MCP call
2. Get TD info via `get_td_info` to confirm Envoy is connected
3. Run `get_externalizations` to check externalization state — report any dirty (unsaved) operators
4. Run `get_op_errors` with `recurse=true` on the root to find any errors in the network
5. Check `dev/logs/` for recent ERROR or WARNING entries
6. Report a concise summary: TD version, Envoy status, externalization health, and any active errors
