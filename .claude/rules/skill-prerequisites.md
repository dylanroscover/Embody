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
| `execute_python`, `set_dat_content`, or `edit_dat_content` (writing TD Python) | `/td-api-reference` |
| Diagnosing operator errors | `/debug-operator` |
| Building or refining any visual / rendered output (generative art, VJ visuals, shaders, scenes, renders, anything shown on screen) | `/visual-aesthetics` |
| `switch_instance` or multi-instance workflows | `/multi-instance` |
| Building or persisting a Specimen (gallery TDN networks) | `/specimen-authoring` |
| First MCP call in a new session | `/mcp-tools-reference` |
