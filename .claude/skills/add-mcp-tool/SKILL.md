---
name: add-mcp-tool
description: "Add a new MCP tool to the Envoy server (Embody development)"
disable-model-invocation: true
---

# Add MCP Tool to Envoy

Steps for adding a new MCP tool:

1. **Add the tool function** inside `_register_tools()` in `EnvoyExt.py`
   - Function signature and docstring define the MCP schema (parameter names, types, descriptions)
   - Treat these as API contracts — changes break client integrations
   - The tool function queues the operation for main-thread execution

2. **Add a handler case** in `_onRefresh()` for the TD operation
   - This is where the actual TouchDesigner operations execute (on the main thread)
   - Wrap TD operations in try/except, return `{'error': str(e)}` dicts on failure
   - Validate all inputs before passing to TD

3. **Update `EXPECTED_ENVOY_TOOLS`** in `dev/embody/unit_tests/test_agent_contract.py`
   - The Tier-1 agent contract test fails on inventory drift in either direction -- same commit as the tool change
   - Tools register on the MCPServer instance at Envoy START -- restart Envoy before believing a mismatch

4. **Update the MCP tools reference**
   - Update the `/mcp-tools-reference` skill in `.claude/skills/mcp-tools-reference/SKILL.md`
   - Update the corresponding template DAT if it exists

5. **Update documentation**
   - Add to the root CLAUDE.md if the tool is significant
   - Update `text_claude.md` template DAT for user projects

6. **Test via MCP Inspector or Claude Code**
   - Verify the tool appears in the tool list
   - Test with valid and invalid inputs
   - Check error handling
