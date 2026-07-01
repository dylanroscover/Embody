# JSON Schema

The TDN format includes a JSON Schema that can be used for validation and IDE auto-completion. `.tdn` files are YAML in v2.0 (a strict JSON superset, so legacy JSON `.tdn` still validate against this schema); the schema validates the parsed structure, which is identical for YAML and JSON sources.

## Schema File

The schema is available at [`tdn.schema.json`](../tdn.schema.json) in the repository.

You can reference it from the top of your `.tdn` files for editor support:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/dylanroscover/Embody/main/docs/tdn.schema.json
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
- **Value types**: Proper typing for constants, expressions (`=` prefix), and binds (`~` prefix)
- **DAT content**: Both text and table formats

## Using with VS Code

With the [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml) installed, add this to your VS Code `settings.json` to get auto-completion and validation for `.tdn` files (v2.0 YAML):

```json
{
  "yaml.schemas": {
    "./docs/tdn.schema.json": ["*.tdn"]
  }
}
```

For legacy JSON `.tdn` files (pre-2.0), use the built-in JSON schema mapping instead:

```json
{
  "json.schemas": [
    {
      "fileMatch": ["*.tdn"],
      "url": "./docs/tdn.schema.json"
    }
  ]
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
├── exported_at: date-time
├── network_path: string
├── options
│   └── include_dat_content: boolean
├── type_defaults: { type → properties }
├── par_templates: { name → [definitions] }
├── operators: [operator]
└── annotations: [annotation]
```

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
├── parameters: { name → value }
├── custom_pars: { page → [definition] | {$t, ...values} }
├── flags: [string]
├── inputs: [string | null]
├── comp_inputs: [string | null]
├── dat_content: string | [[string]]
├── dat_content_format: "text" | "table"
├── children: [operator]
├── annotations: [annotation]
├── palette_clone: boolean
├── tdn_ref: string  (mutually exclusive with children — points to child .tdn)
└── tox_ref: string  (mutually exclusive with children — points to child .tox)
```

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
