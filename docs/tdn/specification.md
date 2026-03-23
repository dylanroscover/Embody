# TDN Specification

**Version 1.1**

TDN (TouchDesigner Network) is a JSON-based file format for representing TouchDesigner operator networks as human-readable, diffable text. It stores only non-default properties, keeping files minimal.

- File extension: `.tdn`
- MIME type: `application/json`
- Encoding: UTF-8
- JSON Schema: [`tdn.schema.json`](../tdn.schema.json)

---

## Document Structure

A `.tdn` file is a JSON object with the following top-level fields:

```json
{
  "format": "tdn",
  "version": "1.1",
  "build": 1,
  "generator": "Embody/5.0.237",
  "td_build": "2025.32050",
  "exported_at": "2025-02-19T12:34:56Z",
  "network_path": "/",
  "type": "containerCOMP",
  "options": {
    "include_dat_content": true
  },
  "type_defaults": { ... },
  "par_templates": { ... },
  "custom_pars": { ... },
  "parameters": { ... },
  "flags": [ ... ],
  "color": [0.3, 0.5, 0.9],
  "tags": ["tdn"],
  "comment": "Main UI container",
  "storage": { ... },
  "operators": [ ... ],
  "annotations": [ ... ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `format` | string | Yes | Always `"tdn"`. Identifies the file format. |
| `version` | string | Yes | Format version. Currently `"1.1"`. |
| `build` | integer | No | Embody build number for the exported COMP. Incremented each time the network is saved via Embody. Useful for version tracking and git diffs. `null` if the COMP has no build tracking. |
| `generator` | string | Yes | Tool that produced the file (e.g., `"Embody/5.0.237"`). |
| `td_build` | string | Yes | TouchDesigner version and build number (e.g., `"2025.32050"`). |
| `exported_at` | string | Yes | ISO 8601 UTC timestamp of export (e.g., `"2025-02-19T12:34:56Z"`). |
| `network_path` | string | Yes | The COMP path represented by this file (e.g., `"/"` for the entire project). |
| `type` | string | No | TouchDesigner operator type of the target COMP (e.g., `"baseCOMP"`, `"containerCOMP"`, `"geometryCOMP"`). Added in v1.1. Makes the file self-describing for portable import into other projects. On import, a mismatch between this field and the destination COMP's type triggers a warning. |
| `options` | object | Yes | Export settings used when generating this file. |
| `options.include_dat_content` | boolean | Yes | Whether DAT text/table content was included in the export. |
| `type_defaults` | object | No | Per-type shared properties (parameters, flags, size, color, tags). See [Type Defaults](#type-defaults). |
| `par_templates` | object | No | Reusable custom parameter page definitions. See [Parameter Templates](#parameter-templates). |
| `custom_pars` | object | No | Target COMP's own custom parameter definitions and values. Same format as operator-level [`custom_pars`](#custom-parameters). Only present if the target COMP has custom parameters. |
| `parameters` | object | No | Target COMP's own non-default built-in parameter values. Same format as operator-level [`parameters`](#parameters). Only present if the target COMP has non-default built-in parameters. |
| `flags` | array | No | Target COMP's own non-default [flags](#flags). Same format as operator-level flags. Added in v1.1. |
| `color` | `[r, g, b]` | No | Target COMP's node color, if different from default gray. RGB floats 0.0–1.0, rounded to 4 decimal places. Added in v1.1. |
| `tags` | array of strings | No | Target COMP's tags, if any. Added in v1.1. |
| `comment` | string | No | Target COMP's node comment, if non-empty. Added in v1.1. |
| `storage` | object | No | Target COMP's persistent [storage entries](#operator-storage). Same format as operator-level storage. Added in v1.1. |
| `operators` | array | Yes | Array of [operator objects](#operator-object). |
| `annotations` | array | No | Array of [annotation objects](#annotations). Only present if the root COMP contains annotations. |

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
  "custom_pars": { ... },
  "flags": [ ... ],
  "storage": { ... },
  "startup_storage": { ... },
  "inputs": [ ... ],
  "comp_inputs": [ ... ],
  "dat_content": "...",
  "dat_content_format": "text",
  "children": [ ... ],
  "palette_clone": true
}
```

### Field Reference

| Field | Type | Required | Condition for inclusion |
|-------|------|----------|------------------------|
| `name` | string | Yes | Always included. The operator's name. |
| `type` | string | Yes | Always included. TouchDesigner operator type (e.g., `"baseCOMP"`, `"noiseTOP"`, `"textDAT"`, `"waveCHOP"`). |
| `position` | `[x, y]` | No | Omitted when `[0, 0]` (default). Included only for operators not at the origin. |
| `size` | `[width, height]` | No | Only if different from the default `[200, 100]`. |
| `color` | `[r, g, b]` | No | Only if different from the default gray `[0.545, 0.545, 0.545]` (tolerance: 0.01 per channel). RGB values are floats from 0.0 to 1.0, rounded to 4 decimal places. |
| `comment` | string | No | Only if non-empty. Annotation text on the node. |
| `tags` | array of strings | No | Only if the operator has tags. |
| `dock` | string | No | Only if the operator is docked to another operator. Sibling name or full path. |
| `parameters` | object | No | Only if there are non-default [built-in parameters](#built-in-parameters) (after [type_defaults](#type-defaults) are factored out). |
| `custom_pars` | object | No | Only if the operator has [custom parameters](#custom-parameters). Dict keyed by page name. |
| `flags` | array | No | Only if any [flags](#flags) differ from their defaults. |
| `storage` | object | No | Only if the operator has non-transient [storage entries](#operator-storage). Dict of key-value pairs. |
| `startup_storage` | object | No | Only if the operator has [startup storage entries](#startup-storage). Values restored via `storeStartupValue()` on import. |
| `inputs` | array | No | Only if the operator has [operator-level connections](#operator-connections). |
| `comp_inputs` | array | No | Only if the operator has [COMP-level connections](#comp-connections). COMPs only. |
| `dat_content` | string or array | No | Only for DAT-family operators when `include_dat_content` is `true`. See [DAT Content](#dat-content). |
| `dat_content_format` | string | No | `"text"` or `"table"`. Present whenever `dat_content` is present. |
| `children` | array | No | Only for COMPs with child operators (excluding palette clones). Contains nested operator objects. See [Children and Hierarchy](#children-and-hierarchy). |
| `annotations` | array | No | Only for COMPs with [annotations](#annotations). Contains annotation objects. |
| `palette_clone` | boolean | No | `true` if this COMP is cloned from the TouchDesigner palette (`/sys/`). When set, children are not exported (TD recreates them from the clone source). |

### Compact Formatting

Short arrays and objects (≤80 characters when inlined) are written on a single line:

```json
"position": [200, -100],
"size": [300, 150],
"color": [0.2, 0.6, 0.9],
"tags": ["audio", "generator"],
"inputs": ["noise1"]
```

Longer arrays remain multi-line. This dramatically reduces file size while maintaining readability.

---

## Built-in Parameters

The `parameters` object maps parameter names to their values. Only built-in (non-custom) parameters whose current value differs from their default are included. Parameters shared unanimously across all operators of a type are factored into [type_defaults](#type-defaults) instead.

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

**Expression** — prefixed with `=`. A Python expression that TouchDesigner evaluates each frame:
```json
"parameters": {
  "tx": "=absTime.frame * 0.1",
  "resizecomp": "=me"
}
```

**Bind** — prefixed with `~`. A reference expression that binds this parameter to another:
```json
"parameters": {
  "tx": "~op('controller').par.posx"
}
```

!!! note
    A fourth mode, **Export**, exists in TouchDesigner but is not stored in TDN. Export mode is set by the exporting operator, not the parameter itself, and cannot be meaningfully imported.

### Escaping

Constant string values that literally start with `=` or `~` are escaped by doubling the prefix:

| Stored value | Meaning |
|-------------|---------|
| `"=foo"` | Expression: `foo` |
| `"==foo"` | Constant string: `"=foo"` |
| `"~bar"` | Bind expression: `bar` |
| `"~~bar"` | Constant string: `"~bar"` |

### Skipped Parameters

The following built-in parameters are never exported, as they are internal actions or not meaningful outside a live project:

**By name:**

- `externaltox`, `enableexternaltox`, `reloadtox`
- `reinitextensions`, `savebackup`
- `savecustom`, `reloadcustom`
- `pageindex`

!!! info
    `file` and `syncfile` parameters ARE exported when non-default, so that TDN files are self-contained for externalized DATs. This ensures file references survive import into a different project.

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

The `custom_pars` object maps page names to arrays of parameter definitions. Unlike built-in parameters, custom parameters are **always fully exported** (including their definitions, ranges, and current values) because the importer must recreate them from scratch.

!!! note
    Only COMPs can have custom parameters in TouchDesigner.

### Page-Grouped Format

Custom parameters are grouped by page name. Each page contains an array of parameter definitions:

```json
"custom_pars": {
  "Controls": [
    {
      "name": "Speed",
      "style": "Float",
      "default": 1,
      "max": 10,
      "clampMin": true,
      "normMax": 5,
      "value": 2.5
    },
    {
      "name": "Mode",
      "style": "Menu",
      "menuNames": ["linear", "ease", "bounce"],
      "menuLabels": ["Linear", "Ease In/Out", "Bounce"],
      "value": 1
    }
  ],
  "About": [
    {
      "name": "Build",
      "style": "Int",
      "label": "Build Number",
      "readOnly": true,
      "value": 14
    }
  ]
}
```

The page name is the dict key — individual parameter definitions do not include a `"page"` field.

### Template References

When a page's parameter definitions match a [parameter template](#parameter-templates), the page is stored as a template reference with value overrides:

```json
"custom_pars": {
  "About": {
    "$t": "about",
    "Build": 14,
    "Date": "2026-02-19 16:09:43 UTC",
    "Touchbuild": "2025.32050"
  }
}
```

The `$t` field names the template. Other keys are parameter value overrides (parameter name → current value). See [Parameter Templates](#parameter-templates).

### Custom Parameter Definition

| Field | Type | Condition | Description |
|-------|------|-----------|-------------|
| `name` | string | Always | Base name of the parameter. For multi-component parameters, this is the group name without any suffix (e.g., `"Pos"` for a group of `Posx`, `Posy`, `Posz`). |
| `label` | string | If different from `name` | Display label shown in the parameter dialog. Omitted when the label matches the parameter name. |
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
| `help` | string | If non-empty | Tooltip help text shown when hovering the parameter in the dialog. Omitted when empty. |
| `value` | any | Single-component, if non-default | Current value. Can be a constant, `"=expr"` string, or `"~bind"` string. Omitted when the value equals the default. |
| `values` | array | Multi-component, if any non-default | Current values for each component. Same format as `value` per element. Omitted when all values equal their defaults. |

### Supported Styles

All 32 parameter styles recognized by TDN:

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

## Type Defaults

The `type_defaults` section hoists properties that are shared unanimously across **all** operators of a given type into a single location, removing them from individual operators. Supported properties: `parameters`, `flags`, `size`, `color`, and `tags`.

```json
"type_defaults": {
  "containerCOMP": {
    "parameters": {
      "borderover": false,
      "reloadbuiltin": false,
      "resizecomp": "=me",
      "repocomp": "=me"
    },
    "flags": ["viewer"],
    "size": [300, 150]
  },
  "textDAT": {
    "parameters": {
      "language": "text"
    },
    "flags": ["viewer"],
    "size": [130, 90],
    "color": [0.67, 0.67, 0.67],
    "tags": ["source"]
  }
}
```

### Unanimity Rule

A property enters `type_defaults` **only** if:

1. The operator type appears 2+ times in the export
2. The property is present on **every** operator of that type
3. The property has the **same value** across all operators of that type

This eliminates the need for a "reset to default" marker — if a property is in `type_defaults`, every operator of that type has it.

### Import Behavior

On import, `type_defaults` are merged into each operator before the relevant import phase. `parameters` use dict-level merge (operator-specific keys override individual defaults). `flags`, `size`, `color`, and `tags` use whole-value replacement (the operator either has its own value or inherits entirely from type_defaults):

```
effective_params = type_defaults[op_type].parameters | operator.parameters
effective_flags  = operator.flags  ?? type_defaults[op_type].flags
effective_size   = operator.size   ?? type_defaults[op_type].size
effective_color  = operator.color  ?? type_defaults[op_type].color
effective_tags   = operator.tags   ?? type_defaults[op_type].tags
```

### When Type Defaults are Omitted

- If no types have 2+ operators with shared properties, the `type_defaults` key is absent
- Single-instance operator types never contribute to type_defaults

---

## Parameter Templates

The `par_templates` section extracts custom parameter page definitions that repeat across 2+ operators into named, reusable templates.

```json
"par_templates": {
  "about": [
    {"name": "Build", "style": "Int", "label": "Build Number", "readOnly": true},
    {"name": "Date", "style": "Str", "label": "Build Date", "readOnly": true},
    {"name": "Touchbuild", "style": "Str", "label": "Touch Build", "readOnly": true}
  ]
}
```

Templates contain parameter definitions **without values** — they define the structure (name, style, label, ranges, etc.) of a page's parameters.

### Template References

Operators reference templates via `$t` in their `custom_pars`:

```json
"custom_pars": {
  "About": {
    "$t": "about",
    "Build": 14,
    "Date": "2026-02-19 16:09:43 UTC",
    "Touchbuild": "2025.32050"
  }
}
```

| Field | Description |
|-------|-------------|
| `$t` | Template name (matches a key in `par_templates`) |
| Other keys | Value overrides: parameter name → current value |

### Import Behavior

On import, `$t` references are resolved before Phase 2 (create custom parameters). Each template reference is expanded back into a full array of parameter definitions, with value overrides applied:

```json
// Resolved from template + overrides:
"About": [
  {"name": "Build", "style": "Int", "label": "Build Number", "readOnly": true, "value": 14},
  {"name": "Date", "style": "Str", "label": "Build Date", "readOnly": true, "value": "2026-02-19 16:09:43 UTC"},
  {"name": "Touchbuild", "style": "Str", "label": "Touch Build", "readOnly": true, "value": "2025.32050"}
]
```

### Template Naming

Template names are derived from the page name (lowercased, spaces replaced with underscores). Collision suffixes (`_2`, `_3`) are added if multiple distinct page definitions share the same page name.

### When Templates are Omitted

- If no page definition appears on 2+ operators, the `par_templates` key is absent
- Pages unique to a single operator are always stored inline

---

## Flags

The `flags` array contains string names of flags whose values differ from their defaults.

| Flag | Default | Description |
|------|---------|-------------|
| `bypass` | `false` | Operator is skipped in the processing chain. |
| `lock` | `false` | Operator output is locked (frozen). See [Lock Flag Limitation](#lock-flag-limitation). |
| `display` | `false` | Marks this operator as the display output (blue flag). |
| `render` | `false` | Marks this operator for rendering (purple flag). |
| `viewer` | `false` | Shows the operator's viewer on its node tile. |
| `expose` | `true` | Whether the node is visible in the network editor. |
| `allowCooking` | `true` | Whether the COMP is allowed to cook. **COMPs only.** |

### Format

Flags that default to `false` are listed by name when set to `true`:
```json
"flags": ["viewer", "display"]
```

Flags that default to `true` use a `-` prefix when set to `false`:
```json
"flags": ["-expose"]
```

Combined example — viewer on, cooking disabled:
```json
"flags": ["viewer", "-allowCooking"]
```

### Lock Flag Limitation

!!! warning "Locked content is NOT preserved for TOPs, CHOPs, or SOPs"

    TDN preserves the **lock flag** for all operator families, but it **cannot store frozen pixel, channel, or geometry data**. After a TDN round-trip (export + import), locked non-DAT operators will be locked but **empty** — no texture, no samples, no mesh.

    **This is by design, not a bug.** Storing binary data would defeat TDN's purpose as a diffable, version-control-friendly format. A single locked 4K TOP could add over 100 MB to a `.tdn` file.

    Embody warns you at save time if your network contains locked non-DAT operators.

The `lock` flag applies to **all** operator families — DATs, TOPs, CHOPs, and SOPs — freezing their cooked output so it no longer updates from inputs or parameters. However, TDN only persists the frozen data for DATs.

| Family | Flag persisted? | Frozen data persisted? | Notes |
|--------|:-:|:-:|---|
| **DAT** | Yes | Yes (via `dat_content`) | Full round-trip: both the lock state and text/table content are preserved. |
| **TOP** | Yes | **No** | Pixel data is not stored. On import, the lock flag is set but no texture data exists. The operator will appear black. |
| **CHOP** | Yes | **No** | Channel data is not stored. On import, the lock flag is set but no sample data exists. |
| **SOP** | Yes | **No** | Geometry data is not stored. On import, the lock flag is set but no mesh data exists. |

**Workarounds:**

- **Unlock before saving** — the operator will re-cook from its inputs on reload.
- **Use TOX strategy** instead of TDN for COMPs containing locked non-DAT operators. TOX files are binary and preserve all locked content.
- **Store data externally** — write pixel data to image files, channel data to CSV, etc., and reference them from your network.

---

## Connections

TouchDesigner operators have two kinds of connections. TDN stores both as string arrays where array position equals the input index.

### Operator Connections

Standard wiring between operators (left/right connectors). Stored in the `inputs` array:

```json
"inputs": ["noise1"]
```

Multi-input example — `noise1` at index 0, nothing at index 1, `level1` at index 2:
```json
"inputs": ["noise1", null, "level1"]
```

### COMP Connections

COMP-level wiring (top/bottom connectors). Only applicable to COMPs. Stored in the `comp_inputs` array:

```json
"comp_inputs": ["container1"]
```

### Source Resolution

Each string element references the source operator:

- If the source operator is a **sibling** (same parent), only the operator **name** is stored (e.g., `"noise1"`).
- If the source is in a **different parent**, the full **path** is stored (e.g., `"/project/other/transform1"`).
- `null` means no connection at that index.

On import, the source is resolved by first looking for a sibling with that name, then falling back to interpreting it as a full path.

---

## Docking

Operators in TouchDesigner can be visually docked to other operators. A docked operator moves with its host in the network editor and can be collapsed into the host's tile.

When an operator is docked, TDN records a `"dock"` field on it:

| Field | Type | Condition |
|-------|------|-----------|
| `dock` | string | Only if the operator is docked to another operator. |

The value is the **sibling name** of the dock host when they share a parent COMP, or the **full operator path** for cross-hierarchy docking. This follows the same reference convention as [operator connections](#connections).

Docking is a purely visual/organizational relationship — it has no effect on operator behavior, data flow, or cooking. It is omitted from `type_defaults` because docking is always instance-specific.

**Example:**

```json
{
  "name": "info1",
  "type": "infoDAT",
  "dock": "noise1"
}
```

During import, the dock target is resolved by sibling name first, then full path fallback. If the target cannot be found, a warning is logged and docking is skipped gracefully.

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

### Inclusion Conditions

DAT content is only included when:

1. The operator belongs to the DAT family
2. The `include_dat_content` option is `true`
3. The DAT has content (non-empty text or at least one row)

---

## Operator Storage

Every TouchDesigner operator has a `.storage` dictionary for persistent Python data. TDN exports all serializable storage entries except known transient/internal keys used by Embody's runtime.

### Format

```json
{
  "name": "my_comp",
  "type": "baseCOMP",
  "storage": {
    "count": 42,
    "label": "hello",
    "items": [1, 2, 3],
    "config": {"key": "value"},
    "coords": {"$type": "tuple", "$value": [10, 20]},
    "tags": {"$type": "set", "$value": ["a", "b", "c"]},
    "raw": {"$type": "bytes", "$value": "AAEC/w=="}
  }
}
```

### Value Serialization

Python types that map directly to JSON are stored as-is. Non-JSON types use a `$type`/`$value` wrapper:

| Python Type | JSON Representation |
|-------------|---------------------|
| `str`, `int`, `float`, `bool` | Direct JSON value |
| `None` | `null` |
| `list` | JSON array (recursive) |
| `dict` | JSON object (recursive, string keys only) |
| `tuple` | `{"$type": "tuple", "$value": [...]}` |
| `set` | `{"$type": "set", "$value": [...]}` (sorted for determinism) |
| `bytes` | `{"$type": "bytes", "$value": "<base64>"}` |

Values that cannot be serialized to JSON (threading objects, operator references, custom class instances) are silently skipped during export.

### Skipped Keys

The following storage keys are never exported (runtime/transient state managed by Embody):

`_tdn_stripped_paths`, `_git_root`, `envoy_running`, `envoy_shutdown_event`, `expanded_paths`, `manage_file_path`, `visible_count`, `hover`

### Startup Storage

TouchDesigner supports `storeStartupValue(key, value)` — values that reset to their initial state on every project open, regardless of what they were when the file was saved. TDN supports this via an optional `startup_storage` field:

```json
{
  "name": "my_comp",
  "type": "baseCOMP",
  "storage": {"runtime_count": 0},
  "startup_storage": {"version": 1, "default_mode": "auto"}
}
```

On import, keys in `startup_storage` are restored via `storeStartupValue()`, while keys in `storage` use `store()`.

**Export limitation:** TouchDesigner provides no API to introspect which storage keys were set with `storeStartupValue()` vs `store()`. During automatic export, all entries go into `storage`. The `startup_storage` field must be populated manually or by tools that know the intent (e.g., code generators, StorageManager-aware exporters).

### Import Behavior

Storage is restored during Phase 6a (after DAT content, before positions). Keys in `storage` are restored via `op.store(key, value)`. Keys in `startup_storage` are restored via `op.storeStartupValue(key, value)`. `$type` wrappers are deserialized back to their Python types. Unknown `$type` values are treated as plain dicts with a warning logged.

---

## Children and Hierarchy

COMPs can contain child operators. These are stored in the `children` array, which contains nested operator objects following the exact same schema:

```json
{
  "name": "container1",
  "type": "baseCOMP",
  "children": [
    {
      "name": "noise1",
      "type": "noiseTOP"
    },
    {
      "name": "null1",
      "type": "nullTOP",
      "position": [300, 0],
      "inputs": ["noise1"]
    }
  ]
}
```

Note that `container1` omits `position` (defaults to `[0, 0]`) and `noise1` also omits `position`. Only `null1` at `[300, 0]` includes its position.

Nesting is recursive — COMPs inside COMPs can have their own `children`. The optional `max_depth` export parameter limits recursion depth (`null` means unlimited). COMPs may also contain an `annotations` array alongside `children` — see [Annotations](#annotations).

### Palette Clones

COMPs that are cloned from the TouchDesigner palette (i.e., their `clone` parameter points to `/sys/`) are marked with `"palette_clone": true`. Their children are **not** exported because TouchDesigner automatically recreates them from the clone source when the project loads.

---

## Annotations

Annotations are visual documentation elements in TouchDesigner networks (comments, network boxes, and annotate panels). They are stored in an `annotations` array at the top level (for root-level annotations) and optionally on each COMP operator (for nested annotations).

```json
{
  "operators": [ ... ],
  "annotations": [
    {
      "name": "annot_core",
      "mode": "annotate",
      "title": "Core Tests",
      "text": "Unit tests for core functionality",
      "position": [-70, -300],
      "size": [1070, 660],
      "color": [0.5, 0.5, 0.5],
      "opacity": 0.8
    }
  ]
}
```

For nested COMPs, `annotations` appears alongside `children`:

```json
{
  "name": "container1",
  "type": "baseCOMP",
  "children": [ ... ],
  "annotations": [
    {
      "name": "annot1",
      "mode": "comment",
      "text": "Signal processing chain"
    }
  ]
}
```

### Annotation Object

| Field | Type | Required | Condition for inclusion |
|-------|------|----------|------------------------|
| `name` | string | Yes | Always included. The annotation operator's name. |
| `mode` | string | Yes | Always included. One of `"annotate"`, `"comment"`, or `"networkbox"`. |
| `title` | string | No | Only if non-empty. Title bar text (for `annotate` and `networkbox` modes). |
| `text` | string | No | Only if non-empty. Body text content. |
| `position` | `[x, y]` | No | Omitted when `[0, 0]` (default). |
| `size` | `[width, height]` | Yes | Always included — annotations have no standard default size. |
| `color` | `[r, g, b]` | No | Only if different from the default gray `[0.545, 0.545, 0.545]`. Background color. |
| `opacity` | number | No | Only if different from `1.0`. Background opacity (0.0 to 1.0). |

### Import Behavior

Annotations are created during Phase 7a (after operator positions, before file link restoration). Each annotation is created as an `annotateCOMP` with `utility=True` (matching TD's native behavior — annotations are utility operators hidden from `.children`).

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
| `str` | string | Stored as-is (with `=`/`~` [escaping](#escaping) applied for parameter values). |
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

Importing a `.tdn` file reconstructs the network in a pre-phase plus eight sequential phases. This ordering ensures that dependencies are satisfied — for example, operators must exist before they can be connected, and positions are set last because creating operators may shift existing nodes.

| Phase | Action | Details |
|-------|--------|---------|
| Pre | **Resolve templates and defaults** | If `par_templates` is present, `$t` references in `custom_pars` are expanded to full definitions with value overrides. If `type_defaults` is present, shared properties are merged into each operator (`parameters` via dict merge, `flags`/`size`/`color`/`tags` via whole-value injection; operator-specific values take precedence). |
| 1 | **Create operators** | All operators are created depth-first. COMPs are created first so their children can be placed inside them. |
| 2 | **Create custom parameters** | Custom parameter definitions are created on COMPs (pages, types, ranges, menu entries, defaults). |
| 3 | **Set parameter values** | Both built-in and custom parameter values are applied. `=` prefix sets expression mode, `~` prefix sets bind mode, all other values set constant mode. |
| 4 | **Set flags** | Operator flags are applied. Array entries without `-` prefix set the flag to `true`; entries with `-` prefix set to `false`. |
| 5 | **Wire connections** | Operator and COMP connections are established. Source references are resolved (sibling name first, then full path). Array position equals input index. |
| 6 | **Set DAT content** | Text or table data is loaded into DAT operators. |
| 6a | **Restore storage** | Storage key-value pairs are restored via `op.store()`. `$type` wrappers are deserialized to Python types (tuple, set, bytes). |
| 7 | **Set positions** | Node positions, sizes, colors, and comments are applied last. Missing position defaults to `[0, 0]`. |
| 7a | **Create annotations** | Annotations are created from the `annotations` array (top-level and per-COMP). Each annotation is created as an `annotateCOMP` with `utility=True`, then its mode, title, body text, position, size, color, and opacity are set. |

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

- **Target COMP metadata** (v1.1+): type, flags, color, tags, comment, storage
- Operator names, types, and hierarchy
- Non-default parameter values (constant, expression, and bind modes)
- Custom parameter definitions (all fields, all styles)
- Flags, connections, positions, sizes, colors, comments, tags
- Operator storage (serializable entries only — see [Operator Storage](#operator-storage))
- Annotations (mode, title, body text, position, size, color, opacity)
- DAT text and table content (byte-for-byte when `include_dat_content` is `true`)
- Float values (stable after the first export — see below)
- Type defaults and parameter templates (re-computed on each export)

### Known Exceptions

**Palette clones** — On first export, a palette-cloned COMP is marked `"palette_clone": true` and its children are skipped. After import, TouchDesigner materializes the children from the clone source. A subsequent re-export will include those children as regular operators. This means the second export is larger than the first.

**Color tolerance** — Colors within `0.01` per channel of the default gray `[0.545, 0.545, 0.545]` are treated as default and not exported. A color of `[0.55, 0.55, 0.55]` survives; `[0.546, 0.546, 0.546]` is dropped.

**Float precision** — Values are rounded to 10 decimal places on first export. This can change the last digits of very precise values (e.g., `3.14159265358979` → `3.1415926536`). After that first rounding, subsequent exports are stable.

**Type defaults recomputation** — Type defaults and parameter templates are recomputed from scratch on each export. If operator populations change between exports (operators added/removed), different properties may qualify as "unanimous" for type_defaults, and different pages may qualify as templates. The final network state is always identical, but the JSON structure may differ.

**Locked non-DAT data** — When a TOP, CHOP, or SOP is locked, TDN preserves the lock flag but not the frozen pixel, channel, or geometry data. After import, the operator is locked but empty. See [Lock Flag Limitation](#lock-flag-limitation).

### Intentionally Excluded

The following are never exported and are not considered a loss:

- **Export-mode parameters** — set by the exporting operator, not the parameter itself
- **Pulse / Momentary / Header styles** — no persistent state
- **Read-only parameters** — cannot be set on import
- **COMP externalization parameters** (`externaltox`, `enableexternaltox`, `reloadtox`) — COMP `.tox` externalization is managed separately by Embody
- **Transient storage keys** — runtime state used by Embody (`envoy_running`, `_git_root`, etc.)
- **Non-serializable storage values** — threading objects, operator references, custom class instances

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
| Target COMP type mismatch (`type` vs destination) | Log a warning, proceed with import. The file's `type` field is informational — import does not change the destination COMP's type. |
| Unknown `$t` template reference | Log a warning, skip that page. |
| Missing `type_defaults` entry for a type | No-op (operator uses its own properties). |
| Non-serializable storage value on export | Skip that value, log at DEBUG level. |
| Unknown `$type` in storage on import | Treat as plain dict, log a warning. |
| Failed `store()` call on import | Skip that key, log a warning. |

### General Principle

Log warnings for anything skipped so the developer can inspect the result. Never abort an entire import because a single operator, parameter, or connection failed — the partial result is more useful than no result.

---

## Complete Example

A realistic `.tdn` file demonstrating all major features:

```json
{
  "format": "tdn",
  "version": "1.1",
  "build": 3,
  "generator": "Embody/5.0.237",
  "td_build": "2025.32050",
  "exported_at": "2026-02-19T14:30:00Z",
  "network_path": "/",
  "type": "baseCOMP",
  "options": {
    "include_dat_content": true
  },
  "type_defaults": {
    "baseCOMP": {
      "parameters": {
        "resizecomp": "=me",
        "repocomp": "=me"
      }
    }
  },
  "par_templates": {
    "about": [
      {"name": "Build", "style": "Int", "label": "Build Number", "readOnly": true},
      {"name": "Version", "style": "Str", "label": "Version", "readOnly": true}
    ]
  },
  "operators": [
    {
      "name": "controller",
      "type": "baseCOMP",
      "color": [0.2, 0.4, 0.8],
      "comment": "Main controller",
      "tags": ["core"],
      "custom_pars": {
        "Controls": [
          {
            "name": "Speed",
            "style": "Float",
            "default": 1,
            "max": 10,
            "clampMin": true,
            "normMax": 5,
            "value": 2.5
          },
          {
            "name": "Mode",
            "style": "Menu",
            "menuNames": ["linear", "ease", "bounce"],
            "menuLabels": ["Linear", "Ease In/Out", "Bounce"],
            "value": 1
          },
          {
            "name": "Color",
            "style": "RGB",
            "clampMin": true,
            "clampMax": true,
            "values": [1, 0.5, 0]
          }
        ],
        "About": {
          "$t": "about",
          "Build": 3,
          "Version": "1.0.0"
        }
      },
      "flags": ["viewer"],
      "comp_inputs": ["renderer"],
      "children": [
        {
          "name": "noise1",
          "type": "noiseTOP",
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
            "opacity": "=parent().par.Speed / 10"
          },
          "inputs": ["noise1"],
          "flags": ["display"]
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
          "flags": ["lock"]
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
      "size": [300, 150],
      "custom_pars": {
        "About": {
          "$t": "about",
          "Build": 1,
          "Version": "0.9.0"
        }
      }
    }
  ]
}
```

Key observations:

- **`type_defaults`**: Both `baseCOMP`s share `resizecomp` and `repocomp` expressions, so those are hoisted out of individual operators
- **`par_templates`**: The "About" page definition is shared between `controller` and `renderer`, with different values
- **Expression shorthand**: `"=parent().par.Speed / 10"` instead of `{"expr": "..."}`
- **Flags as arrays**: `["viewer"]`, `["display"]`, `["lock"]`
- **Simplified connections**: `["noise1"]` instead of `[{"index": 0, "source": "noise1"}]`
- **Optional position**: `noise1` at `[0, 0]` omits `position`; `controller` at `[0, 0]` also omits it
- **Compact formatting**: Arrays like `[300, 0]`, `[0.2, 0.4, 0.8]`, `["core"]` are inline

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-02-19 | Initial release with 8 format optimizations: expression shorthand (`=`/`~` prefixes), flags as arrays, page-grouped custom parameters, type defaults, parameter templates, optional position, simplified connections, compact JSON formatting. |
| 1.0 | 2026-02-22 | Extended `type_defaults` to support `flags`, `size`, `color`, and `tags` in addition to `parameters`. Backward-compatible: old importers ignore unknown keys, new importers handle files without the new keys. |
| 1.0 | 2026-03-01 | Added annotation support (`annotations` array at top level and per-COMP). Added Phase 7a to import process. Removed `file`/`syncfile` from SKIP_PARAMS so DAT file references are preserved in TDN exports. Pre-save now auto-exports current state before stripping TDN COMPs. |
