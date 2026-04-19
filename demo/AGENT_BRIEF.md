# Demo Prompt Agent Brief

You are building a creative TouchDesigner network using the Envoy MCP tools. This is a demo prompt for Embody users — the network itself is the demo, not just the visual output.

## Required Skills (load before starting)

1. `/mcp-tools-reference` — MCP tool catalog
2. `/create-operator` — operator creation workflow
3. `/manage-annotations` — annotation coordinate math
4. `/td-api-reference` — TD Python API reference

## Critical Requirements

### 1. Use Real TD Operators — NOT Just GLSL Shaders

Build with native TD operators: POPs for particles, noiseTOP/noiseCHOP for motion, renderTOP for 3D scenes, feedbackTOP for persistence, compositeTOP for layering, etc. GLSL TOPs are fine for specific shader effects, but the network should demonstrate TD's node-based workflow.

**Prefer POPs over SOPs** — POPs are GPU-accelerated.

### 2. Network Layout (MANDATORY)

Every operator must be placed on the 200-unit grid with proper spacing:

- Call `get_network_layout` BEFORE and AFTER placing operators
- Spacing formula: `next_x = prev_nodeX + prev_nodeWidth + 200` (snap to 200 grid)
- Signal flows LEFT to RIGHT
- Parallel chains go DOWNWARD (decreasing Y, 400 units between rows)
- Batch-compute ALL positions before placing

### 3. Annotations (MANDATORY)

Every operator must be inside an annotation group:

- Create annotations AFTER placing all operators in a group
- Bounding box: find min/max X/Y of operators (including their width/height)
- Padding: 70 units left/right/bottom, 170 units top (title bar)
- `nodeX = min_x - 70`, `nodeY = min_y - 70` (bottom-left corner)
- `nodeWidth = max_x - min_x + 140`, `nodeHeight = max_y - min_y + 240`
- Title describes FUNCTION ("Particle Emission", "Post-Processing"), not implementation

### 4. Error Checking

Call `get_op_errors` with `recurse=true` after creating and connecting operators. Fix all errors.

### 5. Screenshots

Capture the final output: `capture_top` on the output nullTOP. Save to `screenshots/{number}_{name}.png`.

### 6. Performance Check

Call `get_op_performance` on the parent COMP after the network is running. Must run smoothly on a MacBook Air M2.

## ParticlePOP Setup

The particlePOP requires:
1. **Source input** — connect a gridPOP, spherePOP, or other geometry POP to input 0
2. **Feedback loop** — set the `targetpop` parameter to the last POP in the chain (usually a nullPOP). This creates the particle simulation feedback loop.
3. **Initialize** — pulse `initializepulse` after connecting everything
4. The geometryCOMP must have the final nullPOP's display/render flags set

## GLSL TOP Uniform Setup

For custom uniforms like `uTime`:
- Create a `chopToTOP` or use TD's built-in uniforms
- The `const` sequence parameter on glslTOP can be unreliable via Python
- Alternative: use `absTime.seconds` in a parameter expression that feeds into the shader

## Network Structure Pattern

A typical demo network has 3-5 annotation groups flowing left to right:

```
[Source/Emission] → [Forces/Motion] → [Render Scene] → [Post-Processing] → [Output]
```

Each group contains 2-5 operators. Total: 10-100 operators across the network.

## Workspace
- Create a baseCOMP for your demo inside it
- Build everything inside that baseCOMP