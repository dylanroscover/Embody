# Network Layout Conventions

## Placement Procedure (follow every time)

**No exemptions — this governs EVERY operator you create or move, whether through the `create_op` / `create_annotation` MCP tools OR through `execute_python` (`comp.create(...)`, `.copy()`, `.copyOPs()`).** `execute_python` creation is the silent trap: it bypasses the `/create-operator` skill and every gate below, and TD's bare `.create()` drops each new op at **(0, 0)**, stacked on the last. If you create or move operators inside an `execute_python` call, you are on the hook for applying this procedure by hand — above all the **Verify** step, which is the backstop that catches a (0, 0) pileup before it ships. Convenience batching in `execute_python` does not buy you out of layout.

1. **Read first**: `get_network_layout` + `get_annotations` on the parent COMP. Understand existing positions, groups, and flow before touching anything.
2. **Flag problems**: If the existing layout is messy (overlapping ops, orphaned ops outside annotations, broken grid alignment), tell the user before adding to it. Do not silently work around a bad layout.
3. **Identify the target group**: Determine which annotation group the new operator belongs to. If none fits, you will create a new one.
4. **Compute position**: Use actual `nodeWidth`/`nodeHeight` from `get_network_layout`. New operators extend the group to the **right**: `rightmost_nodeX + rightmost_nodeWidth + 200` (snapped to 200-unit grid). If adding a parallel chain, go **down** (lowest Y in the group − 400). Never assume a fixed operator width — operators range from 100 to 300+ units wide.
5. **Batch-compute all positions** before placing anything. Never place one operator, then figure out where the next one goes.
6. **Place, connect, then update the annotation** to enclose the new operators. Use `set_annotation` to expand width/height if needed.
7. **Verify (mandatory gate)**: After placing, call `get_network_layout` again. Confirm: no overlaps (use actual `nodeWidth` -- COMPs are wider than TOPs); **nothing left at (0, 0)**; **every docked op hugs its host**; **every wire flows forward** (each source's `nodeX + nodeWidth` is left of its destination's `nodeX` -- no backward "S" wires); no ops outside annotations; grid alignment intact. **No turn that creates or moves operators may end without this check, and it must run DURING iterative building too -- not just at the very end.** That is exactly when docked DATs scatter and sources end up stacked under their destinations. It is the single backstop, no matter which path (MCP tool or `execute_python`) created the ops.

## Grid and Spacing

- **200-unit grid**: All positions snap to multiples of 200. No arbitrary coordinates.
- **Horizontal spacing**: At least 200 units between the **right edge** of one operator and the **left edge** of the next. Formula: `next_x = prev_nodeX + prev_nodeWidth + 200` (round up to next 200 multiple). Do NOT use a fixed "+300 from nodeX" — operators vary in width.
- **400 units vertical** between parallel chains or annotation groups.
- **Y-axis increases upward**. New rows go downward (decreasing Y). Primary chain at top, secondary chains below.
- **Always use actual dimensions**: `get_network_layout` returns `nodeWidth` and `nodeHeight` for every operator. Use these values, not assumptions.

## Signal Flow

- **Left to right**: Inputs on the left, outputs on the right. New additions go on the far right of their group.
- **Wires must flow forward (positive X) -- never a backward "S" wire.** Every operator must have a higher `nodeX` than each operator feeding it; vertical offset (Y) can go either way. The classic mistake: placing a source at the **same** `nodeX` as its destination (directly above or below it) -- the source's output then sits right of the dest's input, so the wire loops back into an "S". Place each source far enough left that its **right edge** (`nodeX + nodeWidth`) is left of the destination's `nodeX`. Remember COMPs (camera / light / geo) are ~160 wide, not 130. If a wire bends backward, the downstream op is misplaced -- move it right, or move the source left.
- **Branches split vertically**: Branches fan out downward, each continuing left-to-right. Minimize edge crossings.
- **Same row = same stage** in a processing chain (same X). **Same column = same function** across parallel chains (same Y).

## Related Operators Stay Close

Operators that reference each other belong **near each other**, even when no wire or dock connects them -- the reference is a relationship the wires don't draw, and proximity makes it visible.

- A **MAT** sits beside the **Geometry COMP** it shades (a row below, or directly alongside) -- never stranded across the network.
- A **camera** and **light(s)** sit near the **Render TOP** they feed.
- A **CHOP** or **DAT** that drives another op's parameters by reference sits near that op.
- Any COMP named in an `op()` call, a parameter expression, or a material / OP-reference slot sits near the op that references it, when practical.

Rule of thumb: if op A names op B in a parameter, expression, or material slot, a reader should see both without scrolling. Don't park a referenced op in a far corner just because it isn't wired into the chain.

## Docked Callback DATs

**ABSOLUTE RULE -- every docked op hugs its host, every time, no exceptions.** Some operators auto-spawn companion ops **docked** to them (anything in `op.docked`) -- callback DATs, info DATs, shader DATs, keys. After you create ANY op with docked ops, you MUST place EVERY one of its docked ops **directly below the host**; if directly-below is occupied, place it **directly to the host's right**. Never leave a docked op where TD dropped it.

Hosts and what they dock: execute/callback DATs (`chopExecuteDAT`, `datExecuteDAT`, `panelExecuteDAT`, `parameterExecuteDAT`, `executeDAT`); input DATs (`keyboardinDAT`, `mouseinDAT`, `oscinDAT`/`oscoutDAT`); and **GLSL ops** (`glslTOP` / `glslmultiTOP` / `glslMAT`), which dock a **pixel DAT, a compute DAT, AND an info DAT** (the `multi` variants also dock a vertex DAT). TD drops all of these at arbitrary, scattered coordinates -- and `execute_python` / `.create()` scatters them WORSE than `create_op`, so this rule applies in full to both creation paths. **If another op already sits in the slot a docked op needs, move that other op out of the way** -- docked ops take priority and are never threaded around obstacles.

**Layout formula** -- given host op bottom-left (`sx`,`sy`), size (`sw`,`sh`), and `N` docks (use the max `dw`,`dh` across docks for uniform spacing). Docked ops **HUG the host** -- they are the one deliberate exception to 200-grid spacing; place them tight, never a full grid step away:

- Row Y: `row_y = sy - dh - 30` -- a tight ~30-unit gap directly below the host's bottom edge. Do NOT drop a full grid step (200) below; that produces the "stranded op with a long diagonal wire" look, not a docked companion hugging its host.
- Slot step: `step = dw + 20` -- tight, so the docks form one compact cluster directly under the host, not a spread-out row that reaches into a neighbor's column.
- Center the row under the host: dock `i` sits at `nodeX = (sx + sw/2) + (i - (N-1)/2) * step - dw/2`, filling `[L, C, R, ...]` per the table below.
- If `N` tight docks would still reach a neighbor's column, stack the overflow into a second tight row (`row_y - dh - 30`) rather than widening.

| N | Pattern |
|---|---|
| 1 | `[C]` |
| 2 | `[C, R]` |
| 3 | `[L, C, R]` |
| 4 | `[L, C, R, R2]` |
| 5 | `[L2, L, C, R, R2]` |

**Procedure**: After creating ANY op -- whether via `create_op` OR via `execute_python` / `.create()` -- query `op.docked` (`[d.path for d in op('PATH').docked]`), reposition EVERY docked op per the formula using `set_op_position` (or by setting `nodeX`/`nodeY` directly), then `get_network_layout` to confirm the docked row landed directly under the host with no overlap. If a slot collides with any other op (dock or not), **move that other op** to a clear region and recompute -- never thread docks around obstacles, never overlap, never leave one stranded. TDN export captures whatever is live, so this is the only way docked positions stay clean across saves. A `glslTOP` built inside `execute_python` is the classic trap: its pixel / compute / info DATs land scattered across the network and MUST be pulled under the host before you move on -- which the create-operator skill's Verify step must catch.

## Annotations

- **Every operator must be inside exactly one annotation.** No orphans. If a new op doesn't belong to an existing group, create a new annotation for it.
- **Annotations must never overlap each other.** Maintain at least 400 units between annotation edges.
- **Expand annotations when adding operators.** Recalculate the bounding box with padding after every addition.
- **Title names the function, not the implementation.** "Audio Mixing" not "CHOP chain 2". A reader should understand the network from annotation titles alone.

**STOP — you MUST invoke `/manage-annotations` before calling `create_annotation` or `set_annotation`.** It contains required coordinate math. Key point: `nodeX`/`nodeY` is the **bottom-left corner**.

## Complexity Thresholds

- **4–5 annotation groups** in one network: consider breaking into baseCOMPs (or containerCOMPs for UI).
- **15–20 operators** in one group: consider encapsulating into a COMP.
- When encapsulating, the COMP replaces the group in the parent network. Move the annotation title to the COMP's name or label.

## Anti-Patterns

- Placing at `[0, 0]` or the origin without reading existing layout.
- **Creating ops via `execute_python` (`comp.create(...)`) and forgetting they obey this rule.** TD's `.create()` defaults to (0, 0); the `/create-operator` skill and the gates here only fire on the `create_op` tool. Every `execute_python` that creates operators must position them on the grid and end with the **Verify** step — this is the exact bypass that produced a whole sub-COMP stacked at (0, 0).
- Placing "near" a related op by picking a mathematically close but visually wrong position — filling a gap in the middle of a finished row instead of extending rightward.
- **Using fixed offsets like `nodeX + 300` without accounting for `nodeWidth`** — this is the #1 cause of overlapping operators when ops are wider than ~100 units.
- Using TD's `COMP.layout()` — it produces overlapping, unreadable results.
- Creating operators without updating the enclosing annotation.
- Calling `set_op_position` without verifying the target coordinates are clear — `set_op_position` has no overlap detection.
- **Leaving docked callback/info DATs at their auto-spawn position.** TD drops them at arbitrary coordinates. After `create_op` on any op that spawns docks, reposition them per "Docked Callback DATs".
