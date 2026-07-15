# YAML Schema

`.tdn` files are YAML -- and so is this schema. `tdn.schema.yaml` follows the [draft 2020-12 schema standard](https://json-schema.org) for validating structure and driving editor auto-completion.

## Schema File

The schema is available at [`tdn.schema.yaml`](../tdn.schema.yaml) in the repository.

You can reference it from the top of your `.tdn` files for editor support:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/dylanroscover/Embody/main/docs/tdn.schema.yaml
format: tdn
version: '2.0'
# ...
```

## What the Schema Validates

- **Document structure**: Required fields (`format`, `version`, `generator`, `td_build`, `exported_at`, `network_path`, `options`, `operators`)
- **Operator objects**: Name, type, position, size, color, parameters, flags, connections, children
- **Custom parameters**: Page-grouped format, template references (`$t`), all 32 parameter styles
- **Type defaults**: Per-type shared properties (parameters, flags, size, color, tags)
- **Parameter templates**: Reusable custom parameter page definitions
- **Annotations**: Mode, title, text, position, size, color, opacity
- **Value types**: Constants (string, number, or boolean), plus expression (`=` prefix) and bind (`~` prefix) string shorthands
- **DAT content**: Both text and table formats

## Using with VS Code

With the [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml) installed, add this to your VS Code `settings.json`. The `files.associations` entry is what makes VS Code treat `.tdn` files as YAML (they are not a built-in YAML extension) -- without it the YAML extension never activates on your `.tdn` files and the schema does nothing:

```json
{
  "files.associations": {
    "*.tdn": "yaml"
  },
  "yaml.schemas": {
    "./docs/tdn.schema.yaml": ["*.tdn"]
  }
}
```

## Schema Overview

The schema defines these key structures:

### Top-Level Document

```
tdn
├── format: "tdn" (const)
├── version: string
├── build: integer | null
├── generator: string
├── td_build: string
├── source_file: string
├── exported_at: date-time
├── network_path: string
├── options
│   ├── include_dat_content: boolean
│   └── include_storage: boolean
├── type_defaults: { type → properties }
├── par_templates: { name → [definitions] }
├── operators: [operator]
└── annotations: [annotation]
```

When the document represents a single COMP (not a whole project), that COMP's own properties also appear at the root -- `type`, `custom_pars`, `parameters`, `flags`, `color`, `tags`, `comment`, and `storage` (same shapes as on an operator).

### Operator Object

```
operator
├── name: string (required)
├── type: string (required)
├── position: [x, y]
├── size: [width, height]
├── color: [r, g, b]
├── comment: string
├── tags: [string]
├── dock: string  (name of the op this one is docked to)
├── parameters: { name → value }
├── custom_pars: { page → [definition] | {$t, ...values} }
├── flags: [string]
├── storage: object
├── startup_storage: object
├── inputs: [string | null]
├── comp_inputs: [string | null]
├── dat_content: string | [string] | [[string]]
├── dat_content_format: "text" | "table"
├── children: [operator]
├── annotations: [annotation]
├── sequences: { name → [blocks] }
├── dat_read_only: true
├── palette_clone: true
├── tdn_ref: string  (mutually exclusive with children — points to child .tdn)
└── tox_ref: string  (mutually exclusive with children — points to child .tox)
```

A few fields also accept a legacy shape for back-compatibility (e.g. `inputs`/`comp_inputs` as `[{index, source}]`, `flags` as `{flag: boolean}`, a flat `custom_pars` array); the [Specification](specification.md) lists these with their inclusion conditions.

### Annotation Object

```
annotation
├── name: string (required)
├── mode: "annotate" | "comment" | "networkbox" (required)
├── title: string
├── text: string
├── position: [x, y]
├── size: [width, height] (required)
├── color: [r, g, b]
└── opacity: number
```

For the complete field reference with inclusion conditions and default values, see the [Specification](specification.md).
