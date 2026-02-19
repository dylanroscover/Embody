# TDN Format Specification

**Version 1.0**

TDN (TouchDesigner Network) is a JSON-based file format for representing TouchDesigner operator networks as human-readable, diffable text. It stores only non-default properties, keeping files minimal.

File extension: `.tdn`
MIME type: `application/json`
Encoding: UTF-8
JSON Schema: [`tdn.schema.json`](tdn.schema.json)

---

## Table of Contents

- [Document Structure](#document-structure)
- [Operator Object](#operator-object)
- [Built-in Parameters](#built-in-parameters)
- [Custom Parameters](#custom-parameters)
- [Flags](#flags)
- [Connections](#connections)
- [DAT Content](#dat-content)
- [Children and Hierarchy](#children-and-hierarchy)
- [Per-COMP Export Mode](#per-comp-export-mode)
- [Value Serialization](#value-serialization)
- [System Exclusions](#system-exclusions)
- [Import Process](#import-process)
- [Round-Trip Guarantees](#round-trip-guarantees)
- [Error Handling](#error-handling)
- [Complete Example](#complete-example)
- [Changelog](#changelog)

---

## Document Structure

A `.tdn` file is a JSON object with the following top-level fields:

```json
{
  "format": "tdn",
  "version": "1.0",
  "build": 1,
  "generator": "Embody/5.0.79",
  "td_build": "2025.32050",
  "exported_at": "2025-02-09T12:34:56Z",
  "root": "/",
  "options": {
    "include_dat_content": true
  },
  "operators": [ ... ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `format` | string | Yes | Always `"tdn"`. Identifies the file format. |
| `version` | string | Yes | Format version. Currently `"1.0"`. |
| `build` | integer | No | Embody build number for the exported COMP. Incremented each time the network is saved via Embody. Useful for version tracking and git diffs. `null` if the COMP has no build tracking. |
| `generator` | string | Yes | Tool that produced the file (e.g., `"Embody/5.0.79"`). |
| `td_build` | string | Yes | TouchDesigner version and build number (e.g., `"2025.32050"`). |
| `exported_at` | string | Yes | ISO 8601 UTC timestamp of export (e.g., `"2025-02-09T12:34:56Z"`). |
| `root` | string | Yes | The COMP path that was exported (e.g., `"/"` for the entire project). |
| `options` | object | Yes | Export settings used when generating this file. |
| `options.include_dat_content` | boolean | Yes | Whether DAT text/table content was included in the export. |
| `operators` | array | Yes | Array of [operator objects](#operator-object). |

In [per-COMP export mode](#per-comp-export-mode), an additional field is present:

| Field | Type | Description |
|-------|------|-------------|
| `export_mode` | string | `"percomp"` — indicates this file is part of a split export. |

---

## Operator Object

Each entry in the `operators` array (and in nested `children` arrays) is an operator object:

```json
{
  "name": "noise1",
  "type": "noiseTOP",
  "position": [200, -100],
  "size": [300, 150],
  "color": [0.2, 0.6, 0.9],
  "comment": "Primary noise source",
  "tags": ["audio", "generator"],
  "parameters": { ... },
  "custom_pars": [ ... ],
  "flags": { ... },
  "inputs": [ ... ],
  "comp_inputs": [ ... ],
  "dat_content": "...",
  "dat_content_format": "text",
  "children": [ ... ],
  "palette_clone": true,
  "tdn_ref": "path/to/children.tdn"
}
```

### Field Reference

| Field | Type | Required | Condition for inclusion |
|-------|------|----------|------------------------|
| `name` | string | Yes | Always included. The operator's name. |
| `type` | string | Yes | Always included. TouchDesigner operator type (e.g., `"baseCOMP"`, `"noiseTOP"`, `"textDAT"`, `"waveCHOP"`). |
| `position` | `[x, y]` | Yes | Always included. Node tile position in the network editor. |
| `size` | `[width, height]` | No | Only if different from the default `[200, 100]`. |
| `color` | `[r, g, b]` | No | Only if different from the default gray `[0.545, 0.545, 0.545]` (tolerance: 0.01 per channel). RGB values are floats from 0.0 to 1.0, rounded to 4 decimal places. |
| `comment` | string | No | Only if non-empty. Annotation text on the node. |
| `tags` | array of strings | No | Only if the operator has tags. |
| `parameters` | object | No | Only if there are non-default [built-in parameters](#built-in-parameters). |
| `custom_pars` | array | No | Only if the operator has [custom parameters](#custom-parameters). All custom parameters are always included. |
| `flags` | object | No | Only if any [flags](#flags) differ from their defaults. |
| `inputs` | array | No | Only if the operator has [operator-level connections](#operator-connections). |
| `comp_inputs` | array | No | Only if the operator has [COMP-level connections](#comp-connections). COMPs only. |
| `dat_content` | string or array | No | Only for DAT-family operators when `include_dat_content` is `true`. See [DAT Content](#dat-content). |
| `dat_content_format` | string | No | `"text"` or `"table"`. Present whenever `dat_content` is present. |
| `children` | array | No | Only for COMPs with child operators (excluding palette clones). Contains nested operator objects. See [Children and Hierarchy](#children-and-hierarchy). |
| `palette_clone` | boolean | No | `true` if this COMP is cloned from the TouchDesigner palette (`/sys/`). When set, children are not exported (TD recreates them from the clone source). |
| `tdn_ref` | string | No | Only in [per-COMP export mode](#per-comp-export-mode). Replaces `children` with a path to a separate `.tdn` file. |

---

## Built-in Parameters

The `parameters` object maps parameter names to their values. Only built-in (non-custom) parameters whose current value differs from their default are included.

### Parameter Modes

Parameters can be in one of three exportable modes:

**Constant** — the value is stored directly:
```json
"parameters": {
  "tx": 100,
  "name": "hello",
  "active": true
}
```

**Expression** — a Python expression that TouchDesigner evaluates each frame:
```json
"parameters": {
  "tx": { "expr": "absTime.frame * 0.1" }
}
```

**Bind** — a reference expression that binds this parameter to another:
```json
"parameters": {
  "tx": { "bind": "op('controller').par.posx" }
}
```

A fourth mode, **Export**, exists in TouchDesigner but is not stored in TDN. Export mode is set by the exporting operator, not the parameter itself, and cannot be meaningfully imported.

### Skipped Parameters

The following built-in parameters are never exported, as they are managed by the externalization system or are not meaningful outside a live project:

**By name:**
- `externaltox`, `enableexternaltox`, `reloadtox`
- `file`, `syncfile`
- `reinitextensions`, `savebackup`
- `savecustom`, `reloadcustom`
- `pageindex`

**By style:**
- `Pulse` — action buttons (fire-once, no persistent state)
- `Momentary` — momentary buttons (no persistent state)
- `Header` — visual section dividers (no value)

**Other exclusions:**
- Read-only parameters
- Custom parameters (handled separately in `custom_pars`)

### Non-Default Comparison

A constant parameter is included only if its current value differs from its default:

- **Floats**: considered different if `abs(current - default) > 1e-9`
- **OP-reference parameters**: `None` and `""` are treated as equivalent (both mean "no operator connected")
- **All other types**: standard equality comparison (`!=`)

---

## Custom Parameters

The `custom_pars` array contains definitions for all custom parameters on an operator. Unlike built-in parameters, custom parameters are **always fully exported** (including their definitions, ranges, and current values) because the importer must recreate them from scratch.

> **Note:** Only COMPs can have custom parameters in TouchDesigner.

### Custom Parameter Object

```json
{
  "name": "Speed",
  "label": "Movement Speed",
  "page": "Controls",
  "style": "Float",
  "default": 5,
  "max": 10,
  "clampMin": true,
  "startSection": true,
  "value": 3.5
}
```

Unlike built-in parameters, custom parameter definitions use a minimal representation — fields are only included when they differ from standard defaults. This keeps the output compact while retaining all information needed to reconstruct the parameter.

| Field | Type | Condition | Description |
|-------|------|-----------|-------------|
| `name` | string | Always | Base name of the parameter. For multi-component parameters, this is the group name without any suffix (e.g., `"Pos"` for a group of `Posx`, `Posy`, `Posz`). |
| `label` | string | If different from `name` | Display label shown in the parameter dialog. Omitted when the label matches the parameter name. |
| `page` | string | Always | Name of the custom parameter page (e.g., `"Custom"`). |
| `style` | string | Always | Parameter style. See [Supported Styles](#supported-styles). |
| `size` | integer | Multi-component `Float`/`Int` only | Number of components when > 1 (e.g., `3` for a 3-component float). |
| `default` | any | If non-standard | Default value. Omitted when the default is a standard value (`0`, `0.0`, `""`, or `false`). |
| `min` | number | If != `0` | Minimum value. |
| `max` | number | If != `1` | Maximum value. |
| `clampMin` | boolean | If `true` | Whether the value is clamped to `min`. |
| `clampMax` | boolean | If `true` | Whether the value is clamped to `max`. |
| `normMin` | number | If != `0` | Normalized range minimum (for slider UI). |
| `normMax` | number | If != `1` | Normalized range maximum (for slider UI). |
| `menuNames` | array of strings | Manually defined menus | Internal names for each menu option. |
| `menuLabels` | array of strings | If different from `menuNames` | Display labels for each menu option. Omitted when labels match names. |
| `menuSource` | string | Dynamically populated menus | DAT path or expression that populates the menu. When present, `menuNames`/`menuLabels` are omitted. |
| `startSection` | boolean | If `true` | Whether this parameter starts a new visual section. |
| `readOnly` | boolean | If `true` | Whether the parameter is read-only. |
| `value` | any | Single-component, if non-default | Current value. Can be a constant, `{"expr": "..."}`, or `{"bind": "..."}`. Omitted when the value equals the default. |
| `values` | array | Multi-component, if any non-default | Current values for each component. Same format as `value` per element. Omitted when all values equal their defaults. |

### Supported Styles

All 25 parameter styles recognized by TDN:

| Style | Category | Description |
|-------|----------|-------------|
| `Float` | Numeric | Floating-point number. Supports `size` > 1 for multi-component (suffixed `1`, `2`, `3`...). |
| `Int` | Numeric | Integer number. Supports `size` > 1 for multi-component (suffixed `1`, `2`, `3`...). |
| `XY` | Numeric compound | Two-component float (`x`, `y`). |
| `XYZ` | Numeric compound | Three-component float (`x`, `y`, `z`). |
| `XYZW` | Numeric compound | Four-component float (`x`, `y`, `z`, `w`). |
| `WH` | Numeric compound | Two-component float (`w`, `h`). |
| `UV` | Numeric compound | Two-component float (`u`, `v`). |
| `UVW` | Numeric compound | Three-component float (`u`, `v`, `w`). |
| `RGB` | Numeric compound | Three-component float (`r`, `g`, `b`). |
| `RGBA` | Numeric compound | Four-component float (`r`, `g`, `b`, `a`). |
| `Str` | String | Text string. |
| `Menu` | Menu | Dropdown menu. Uses `menuNames`/`menuLabels` for static menus, or `menuSource` for dynamic menus. |
| `StrMenu` | Menu | Editable string with dropdown suggestions. Uses `menuNames`/`menuLabels` or `menuSource`. |
| `Toggle` | Boolean | On/off checkbox. |
| `Pulse` | Action | Fire-once button (no persistent value). |
| `Momentary` | Action | Button that is active while held. |
| `Header` | Visual | Section header label (no value). |
| `File` | Path | File path selector (open). |
| `FileSave` | Path | File path selector (save). |
| `Folder` | Path | Folder path selector. |
| `Python` | Code | Python expression field. |
| `OP` | Reference | Operator path reference (any type). |
| `COMP` | Reference | COMP operator reference. |
| `TOP` | Reference | TOP operator reference. |
| `CHOP` | Reference | CHOP operator reference. |
| `SOP` | Reference | SOP operator reference. |
| `DAT` | Reference | DAT operator reference. |
| `MAT` | Reference | MAT operator reference. |
| `POP` | Reference | POP operator reference. |
| `Object` | Reference | Object COMP reference. |
| `PanelCOMP` | Reference | Panel COMP reference. |
| `Sequence` | Sequence | Sequence block parameter. |

### Multi-Component Parameters

Some parameters consist of multiple related components grouped together (called "tuplets" in TouchDesigner).

**Compound styles** (XY, XYZ, XYZW, WH, UV, UVW, RGB, RGBA) have named suffixes:

```json
{
  "name": "Pos",
  "style": "XYZ",
  "values": [10.0, 20.0, 30.0]
}
```

This creates three parameters: `Posx`, `Posy`, `Posz`. The suffix mappings are:

| Style | Suffixes |
|-------|----------|
| `XY` | `x`, `y` |
| `XYZ` | `x`, `y`, `z` |
| `XYZW` | `x`, `y`, `z`, `w` |
| `WH` | `w`, `h` |
| `UV` | `u`, `v` |
| `UVW` | `u`, `v`, `w` |
| `RGB` | `r`, `g`, `b` |
| `RGBA` | `r`, `g`, `b`, `a` |

**Numeric multi-component** (Float or Int with `size` > 1) use numeric suffixes:

```json
{
  "name": "Weight",
  "style": "Float",
  "size": 3,
  "values": [0.5, 0.3, 0.2]
}
```

This creates three parameters: `Weight1`, `Weight2`, `Weight3`.

---

## Flags

The `flags` object contains boolean toggles that control operator behavior. Only flags differing from their defaults are included.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `bypass` | boolean | `false` | Operator is skipped in the processing chain. Input passes through unchanged. |
| `lock` | boolean | `false` | DAT content is locked and will not update when the operator recooks. |
| `display` | boolean | `false` | Marks this operator as the display output of its network (the "blue flag"). |
| `render` | boolean | `false` | Marks this operator for rendering (the "purple flag"). |
| `viewer` | boolean | `false` | Shows the operator's viewer on its node tile in the network editor. |
| `expose` | boolean | `true` | Whether the node is visible in the network editor. Set to `false` to hide it. |
| `allowCooking` | boolean | `true` | Whether the COMP and its children are allowed to cook. **COMPs only** — this flag is not exported for non-COMP operators. |

Example — a bypassed operator with its viewer shown:
```json
"flags": {
  "bypass": true,
  "viewer": true
}
```

---

## Connections

TouchDesigner operators have two kinds of connections. TDN stores both.

### Operator Connections

Standard wiring between operators (left/right connectors in the network editor). Stored in the `inputs` array:

```json
"inputs": [
  { "index": 0, "source": "noise1" },
  { "index": 1, "source": "/project/other/transform1" }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `index` | integer | Input connector index on the destination operator (0-based). |
| `source` | string | Reference to the source operator. |

### COMP Connections

COMP-level wiring (top/bottom connectors). Only applicable to COMPs. Stored in the `comp_inputs` array:

```json
"comp_inputs": [
  { "index": 0, "source": "container1" }
]
```

Same field structure as operator connections.

### Source Resolution

The `source` field uses **relative naming** when possible:

- If the source operator is a **sibling** (same parent), only the operator **name** is stored (e.g., `"noise1"`).
- If the source is in a **different parent**, the full **path** is stored (e.g., `"/project/other/transform1"`).

On import, the source is resolved by first looking for a sibling with that name, then falling back to interpreting it as a full path.

---

## DAT Content

DAT-family operators can optionally include their text or table data. This is controlled by the `include_dat_content` option.

### Text Format

For text-based DATs (textDAT, etc.):

```json
{
  "name": "script1",
  "type": "textDAT",
  "dat_content": "print('hello world')\nprint('goodbye')",
  "dat_content_format": "text"
}
```

- `dat_content`: raw text string with newlines
- `dat_content_format`: `"text"`

### Table Format

For table-based DATs (tableDAT, etc.):

```json
{
  "name": "lookup1",
  "type": "tableDAT",
  "dat_content": [
    ["name", "value", "type"],
    ["speed", "1.5", "float"],
    ["active", "1", "int"]
  ],
  "dat_content_format": "table"
}
```

- `dat_content`: array of row arrays (each row is an array of cell value strings)
- `dat_content_format`: `"table"`

DAT content is only included when:
1. The operator belongs to the DAT family
2. The `include_dat_content` option is `true`
3. The DAT has content (non-empty text or at least one row)

---

## Children and Hierarchy

COMPs can contain child operators. These are stored in the `children` array, which contains nested operator objects following the exact same schema:

```json
{
  "name": "container1",
  "type": "baseCOMP",
  "position": [0, 0],
  "children": [
    {
      "name": "noise1",
      "type": "noiseTOP",
      "position": [0, 0]
    },
    {
      "name": "null1",
      "type": "nullTOP",
      "position": [300, 0],
      "inputs": [
        { "index": 0, "source": "noise1" }
      ]
    }
  ]
}
```

Nesting is recursive — COMPs inside COMPs can have their own `children`. The optional `max_depth` export parameter limits recursion depth (`null` means unlimited).

### Palette Clones

COMPs that are cloned from the TouchDesigner palette (i.e., their `clone` parameter points to `/sys/`) are marked with `"palette_clone": true`. Their children are **not** exported because TouchDesigner automatically recreates them from the clone source when the project loads.

---

## Per-COMP Export Mode

In per-COMP mode, the network is split into multiple `.tdn` files instead of one monolithic file. Each COMP that has children gets its own file.

### How it Works

A COMP's `children` array is replaced by a `tdn_ref` string pointing to a separate file:

```json
{
  "name": "controller",
  "type": "baseCOMP",
  "position": [0, 0],
  "tdn_ref": "controller.tdn"
}
```

The referenced file is a full `.tdn` document with its own metadata headers and an `operators` array containing what would have been the `children`.

### File Naming

- **Root file**: `{project_name}.tdn` (when exporting from `/`) or `{comp_path}.tdn`
- **Child files**: `{comp_td_path}.tdn` where the TD path is stripped of the leading `/`

Example file tree for a project named `MyProject`:

```
MyProject.tdn                        # root operators
controller.tdn                       # /controller's children
controller/engine.tdn                # /controller/engine's children
renderer.tdn                         # /renderer's children
```

Per-COMP files include `"export_mode": "percomp"` in their top-level metadata.

> **Note:** The `import_network` tool operates on a single `.tdn` document. When importing per-COMP files, the caller must load and resolve `tdn_ref` references — either by importing each file separately into its target COMP, or by reassembling the full `children` hierarchy before import.

---

## Value Serialization

All parameter and content values are converted to JSON-safe types using these rules, applied in order:

| Python Type | JSON Output | Rule |
|-------------|-------------|------|
| `None` | string | Converted to empty string `""`. |
| `bool` | boolean | Stored as-is (`true`/`false`). |
| `int` | number | Stored as-is. |
| `float` | number (int) | If the value is a whole number (and fits in 53-bit integer range), it is converted to an integer. E.g., `1.0` becomes `1`. |
| `float` | number (float) | Rounded to 10 decimal places to eliminate floating-point noise. |
| `str` | string | Stored as-is. |
| `list` / `tuple` | array | Each element is recursively serialized. |
| Any other type | string | Converted via `str()`. |

**Color values** (`color` field on operators) are rounded to 4 decimal places.

---

## System Exclusions

The following top-level paths and all their descendants are always excluded from export. These contain TouchDesigner system internals that should not be version-controlled:

| Path | Contents |
|------|----------|
| `/local` | Local parameters |
| `/sys` | System operators (Thread Manager, TDJSON, etc.) |
| `/perform` | Performance monitoring |
| `/ui` | UI framework operators |

An operator is excluded if its path equals one of these or starts with one followed by `/` (e.g., `/sys/TDResources` is excluded).

---

## Import Process

Importing a `.tdn` file reconstructs the network in seven sequential phases. This ordering ensures that dependencies are satisfied — for example, operators must exist before they can be connected, and positions are set last because creating operators may shift existing nodes.

| Phase | Action | Details |
|-------|--------|---------|
| 1 | **Create operators** | All operators are created depth-first. COMPs are created first so their children can be placed inside them. |
| 2 | **Create custom parameters** | Custom parameter definitions are created on COMPs (pages, types, ranges, menu entries, defaults). |
| 3 | **Set parameter values** | Both built-in and custom parameter values are applied (constants, expressions, and bind expressions). |
| 4 | **Set flags** | Operator flags (bypass, lock, display, etc.) are applied. |
| 5 | **Wire connections** | Operator and COMP connections are established. Source references are resolved (sibling name first, then full path). |
| 6 | **Set DAT content** | Text or table data is loaded into DAT operators. |
| 7 | **Set positions** | Node positions, sizes, colors, and comments are applied last. |

The importer accepts either a full `.tdn` document (with metadata) or just the `operators` array directly.

### Version Compatibility

When importing a full `.tdn` document, the importer checks the metadata fields for compatibility:

- **`version`**: Compared against the current TDN format version. A warning is logged if they differ, indicating the file may use a newer or older schema.
- **`td_build`**: Compared against the running TouchDesigner version. An informational message is logged if they differ, since operator types and parameter defaults may vary between TD builds.
- **`build`**: Logged for informational purposes, identifying which save iteration is being imported.

These checks are non-blocking — the import always proceeds regardless of mismatches.

---

## Round-Trip Guarantees

For most networks, export → import → re-export produces identical `.tdn` output. The format is designed to be stable across round-trips, with a few documented exceptions.

### Preserved

- Operator names, types, and hierarchy
- Non-default parameter values (constant, expression, and bind modes)
- Custom parameter definitions (all fields, all styles)
- Flags, connections, positions, sizes, colors, comments, tags
- DAT text and table content (byte-for-byte when `include_dat_content` is `true`)
- Float values (stable after the first export — see below)

### Known Exceptions

**Palette clones** — On first export, a palette-cloned COMP is marked `"palette_clone": true` and its children are skipped. After import, TouchDesigner materializes the children from the clone source. A subsequent re-export will include those children as regular operators. This means the second export is larger than the first.

**Color tolerance** — Colors within `0.01` per channel of the default gray `[0.545, 0.545, 0.545]` are treated as default and not exported. A color of `[0.55, 0.55, 0.55]` survives; `[0.546, 0.546, 0.546]` is dropped.

**Float precision** — Values are rounded to 10 decimal places on first export. This can change the last digits of very precise values (e.g., `3.14159265358979` → `3.1415926536`). After that first rounding, subsequent exports are stable.

### Intentionally Excluded

The following are never exported and are not considered a loss:

- **Export-mode parameters** — set by the exporting operator, not the parameter itself
- **Pulse / Momentary / Header styles** — no persistent state
- **Read-only parameters** — cannot be set on import
- **Embody-managed parameters** (`file`, `syncfile`, `externaltox`, etc.) — managed by the externalization system

---

## Error Handling

TDN import is **best-effort** — individual failures should not abort the entire operation. This section describes the expected behavior for developers working with TDN files.

### Unknown Fields

Developers should ignore unknown fields when parsing TDN documents. This ensures forward compatibility — a file exported by a newer version of Embody can still be imported by an older version, with unrecognized fields silently skipped.

### Failure Modes

| Situation | Expected behavior |
|-----------|-------------------|
| Unknown field in any object | Ignore it. |
| Missing required field (`name`, `type`) on an operator | Skip that operator, log an error. |
| Missing connection source (operator not found) | Skip that connection, log a warning. |
| Unrecognized custom parameter `style` | Skip that parameter definition, log a warning. |
| Unrecognized flag name | Ignore it. |
| Invalid parameter value type | Attempt type coercion; if impossible, skip with a warning. |
| Version mismatch (`version`, `td_build`) | Log a warning, proceed with import. |

### General Principle

Log warnings for anything skipped so the developer can inspect the result. Never abort an entire import because a single operator, parameter, or connection failed — the partial result is more useful than no result.

---

## Complete Example

A realistic `.tdn` file demonstrating all major features:

```json
{
  "format": "tdn",
  "version": "1.0",
  "build": 3,
  "generator": "Embody/5.0.79",
  "td_build": "2025.32050",
  "exported_at": "2025-02-09T14:30:00Z",
  "root": "/",
  "options": {
    "include_dat_content": true
  },
  "operators": [
    {
      "name": "controller",
      "type": "baseCOMP",
      "position": [0, 0],
      "color": [0.2, 0.4, 0.8],
      "comment": "Main controller",
      "tags": ["core"],
      "custom_pars": [
        {
          "name": "Speed",
          "page": "Controls",
          "style": "Float",
          "default": 1,
          "max": 10,
          "clampMin": true,
          "normMax": 5,
          "value": 2.5
        },
        {
          "name": "Mode",
          "page": "Controls",
          "style": "Menu",
          "menuNames": ["linear", "ease", "bounce"],
          "menuLabels": ["Linear", "Ease In/Out", "Bounce"],
          "value": 1
        },
        {
          "name": "Color",
          "page": "Controls",
          "style": "RGB",
          "clampMin": true,
          "clampMax": true,
          "values": [1, 0.5, 0]
        }
      ],
      "flags": {
        "viewer": true
      },
      "comp_inputs": [
        { "index": 0, "source": "renderer" }
      ],
      "children": [
        {
          "name": "noise1",
          "type": "noiseTOP",
          "position": [0, 0],
          "parameters": {
            "type": "sparse",
            "amp": 0.8,
            "period": 2,
            "monochrome": true,
            "resolutionw": 1920,
            "resolutionh": 1080
          }
        },
        {
          "name": "level1",
          "type": "levelTOP",
          "position": [300, 0],
          "parameters": {
            "opacity": { "expr": "parent().par.Speed / 10" }
          },
          "inputs": [
            { "index": 0, "source": "noise1" }
          ],
          "flags": {
            "display": true
          }
        },
        {
          "name": "config",
          "type": "tableDAT",
          "position": [0, -200],
          "dat_content": [
            ["key", "value"],
            ["resolution", "1920x1080"],
            ["fps", "60"]
          ],
          "dat_content_format": "table",
          "flags": {
            "lock": true
          }
        },
        {
          "name": "script1",
          "type": "textDAT",
          "position": [300, -200],
          "dat_content": "# Initialize\nprint('Controller ready')",
          "dat_content_format": "text"
        }
      ]
    },
    {
      "name": "renderer",
      "type": "baseCOMP",
      "position": [500, 0],
      "size": [300, 150]
    }
  ]
}
```

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-02-09 | Initial release. |
