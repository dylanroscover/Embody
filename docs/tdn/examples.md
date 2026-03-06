# Examples

## Complete Example

A realistic `.tdn` file demonstrating all major features:

```json
{
  "format": "tdn",
  "version": "1.0",
  "build": 3,
  "generator": "Embody/5.0.93",
  "td_build": "2025.32050",
  "exported_at": "2026-02-19T14:30:00Z",
  "network_path": "/",
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

## Key Observations

### Type Defaults
Both `baseCOMP`s share `resizecomp` and `repocomp` expressions, so those are hoisted into `type_defaults` instead of being repeated on each operator.

### Parameter Templates
The "About" page definition (Build + Version parameters) is shared between `controller` and `renderer`, so the structure is defined once in `par_templates` and each operator references it with `$t` and provides its own values.

### Expression Shorthand
`"=parent().par.Speed / 10"` — expressions are prefixed with `=` instead of wrapped in an object like `{"expr": "..."}`.

### Flags as Arrays
`["viewer"]`, `["display"]`, `["lock"]` — compact array format instead of `{"viewer": true}`.

### Simplified Connections
`["noise1"]` — just the source operator name. Array position equals input index.

### Optional Position
`noise1` at `[0, 0]` omits `position` entirely. Only operators not at the origin include their position.

### Compact Formatting
Short arrays like `[300, 0]`, `[0.2, 0.4, 0.8]`, `["core"]` are inlined on a single line.

## Minimal Example

The simplest possible `.tdn` file — a single operator with no custom settings:

```json
{
  "format": "tdn",
  "version": "1.0",
  "generator": "Embody/5.0.140",
  "td_build": "2025.32280",
  "exported_at": "2026-03-01T12:00:00Z",
  "network_path": "/project1",
  "options": {
    "include_dat_content": false
  },
  "operators": [
    {
      "name": "noise1",
      "type": "noiseTOP"
    }
  ]
}
```

## Annotation Example

A network with annotations grouping related operators:

```json
{
  "format": "tdn",
  "version": "1.0",
  "generator": "Embody/5.0.140",
  "td_build": "2025.32280",
  "exported_at": "2026-03-01T12:00:00Z",
  "network_path": "/project1/main",
  "options": {
    "include_dat_content": true
  },
  "operators": [
    {
      "name": "noise1",
      "type": "noiseTOP"
    },
    {
      "name": "level1",
      "type": "levelTOP",
      "position": [300, 0],
      "inputs": ["noise1"]
    },
    {
      "name": "config",
      "type": "tableDAT",
      "position": [0, -400],
      "dat_content": [
        ["key", "value"],
        ["fps", "60"]
      ],
      "dat_content_format": "table"
    }
  ],
  "annotations": [
    {
      "name": "annot_visuals",
      "mode": "annotate",
      "title": "Visual Processing",
      "text": "Noise generation and level adjustment",
      "position": [-70, -170],
      "size": [670, 440]
    },
    {
      "name": "annot_config",
      "mode": "annotate",
      "title": "Configuration",
      "text": "Project settings",
      "position": [-70, -570],
      "size": [370, 340]
    }
  ]
}
```
