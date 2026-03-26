---
description: "Skill loading requirements before MCP tool calls — must load the relevant skill BEFORE acting"
---

# Skill Prerequisites

Skills are prerequisites, not optional reference. **Load the relevant skill BEFORE acting:**

| Before calling | Load skill |
|---|---|
| `create_op` | `/create-operator` |
| `create_annotation` or `set_annotation` | `/manage-annotations` |
| `create_extension` | `/create-extension` |
| `externalize_op` or `save_externalization` | `/externalize-operator` |
| `execute_python` or `set_dat_content` (writing TD Python) | `/td-api-reference` |
| Diagnosing operator errors | `/debug-operator` |
| `switch_instance` or multi-instance workflows | `/multi-instance` |
| First MCP call in a new session | `/mcp-tools-reference` |

When updating a rule or skill in `.claude/`, also update the corresponding template DAT in `dev/embody/Embody/templates/` if one exists. Root CLAUDE.md and `text_claude.md` serve different audiences and are maintained independently.
