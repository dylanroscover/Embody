---
description: "Skill loading requirements before MCP tool calls -- must load the relevant skill BEFORE acting"
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
| Fetching data over HTTP, or any background / long-running / blocking task | `/td-api-reference` (Background and Long-Running Work) |
| Recording, exporting, or batch-encoding any movie or image sequence | `/movie-export` |
| Creating or designing custom parameters on any COMP | `/parameter-design` |
| Connectivity broken beyond ~15s of self-heal waiting | `/td-recovery` |
| The moment a `_peers` advisory or a second session appears | `/multi-session-etiquette` |
| Diagnosing operator errors | `/debug-operator` |
| Building or refining any visual / rendered output (generative art, VJ visuals, shaders, scenes, renders, anything shown on screen) | `/visual-aesthetics` |
| Creating or editing POP operators, particle systems, GPU point/geometry work, glslPOP compute, or converting SOP chains to POPs | `/pop-networks` |
| Building or styling a TD panel UI (dialog, wizard, HUD, control panel, buttons/text) | `/build-ui` (design system) + `rules/td-ui.md` (mechanics) |
| `switch_instance` or multi-instance workflows | `/multi-instance` |
| Building or persisting a Specimen (gallery TDN networks) | `/specimen-authoring` |
| First MCP call in a new session | `/mcp-tools-reference` |
