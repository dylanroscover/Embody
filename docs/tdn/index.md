# TDN Format

**TDN** (TouchDesigner Network) is the substrate that makes the rest of Embody possible. It's a YAML-based file format for representing TouchDesigner operator networks as text — text your AI agent can read, text any diff tool can compare, text a network can rebuild itself from. Unlike binary `.toe` and `.tox` files, a `.tdn` file is the network in a form anything can understand.

## Why TDN?

Without a text format for networks, AI-driven TouchDesigner work is one-directional: you generate, and you're stuck with what you got. There's no way to compare attempts, no way to revert cleanly, no way to hand the agent a snapshot of what's already on screen. TDN closes that loop. The format is designed to be as **lean and efficient as possible** — both in file size and readability:

- **Non-default only** — only parameters that differ from their defaults are exported. No bloat, no noise — just what you actually changed
- **Human-readable YAML** — easy to read, diff, and review (in pull requests or any text comparison tool); multi-line scripts render as literal block scalars instead of escaped strings
- **Aggressive deduplication** — shared properties are hoisted into type defaults and parameter templates, eliminating redundancy across operators
- **Round-trip fidelity** — export a network, modify the YAML, import it back with identical results

TDN is designed from the ground up to produce the **smallest possible output** while remaining fully readable. Every design decision — from shorthand prefixes to compact formatting — serves this goal.

## Key Design Principles

### Compact Shorthands

TDN uses prefix characters instead of verbose wrapper objects:

| Prefix | Meaning | Example | Instead of |
|--------|---------|---------|------------|
| `=` | Expression | `"=absTime.frame * 0.1"` | `{"mode": "expression", "expr": "absTime.frame * 0.1"}` |
| `~` | Bind | `"~op('ctrl').par.x"` | `{"mode": "bind", "bindExpr": "op('ctrl').par.x"}` |
| `-` | Negated flag | `"-expose"` | `{"expose": false}` |
| `==` | Escaped `=` | `"==literal"` | Constant string that starts with `=` |
| `~~` | Escaped `~` | `"~~literal"` | Constant string that starts with `~` |

No prefix means a constant value. This keeps the common case (constant parameters) as clean as possible.

### Deduplication

- **Type defaults**: Properties shared across all operators of a given type are hoisted into a top-level `type_defaults` section — removed from each individual operator
- **Parameter templates**: Identical custom parameter page definitions appearing on 2+ operators are extracted into a `par_templates` section and referenced via `$t`
- **Compact arrays**: Short numeric vectors (`position`, `size`, `color` — up to four elements) are inlined with YAML flow style (`[200, -100]`); longer or non-numeric sequences use block style
- **Simplified connections**: `["noise1"]` instead of `[{"index": 0, "source": "noise1"}]` — array position equals input index
- **Optional position**: Operators at `[0, 0]` omit the `position` field entirely
- **Flags as arrays**: `["viewer", "display"]` instead of `{"viewer": true, "display": true}` — only non-default flags are listed

## File Format

- Extension: `.tdn`
- MIME type: `application/yaml`
- Encoding: UTF-8
- Schema: [`tdn.schema.yaml`](https://github.com/dylanroscover/Embody/blob/main/docs/tdn.schema.yaml) — validates the parsed structure

## Usage

### Read (live, no disk)

Use the `read_tdn` MCP tool to return a live network as a TDN dict without writing anything to disk. Preferred for LLM workflows exploring multi-operator networks — **~20-90× fewer tokens** than walking the same subtree with `get_op` + `query_network`.

- `comp_path` — Starting COMP (default: `/`)
- `include_dat_content` — Include DAT text/table content
- `max_depth` — Cap recursion on large roots
- `embed_all` — Recurse into TDN-tagged COMPs instead of skipping their children

Works in all three `Tdnmode` values. See [Import & Export → Reading a Network](import-export.md#reading-a-network-no-disk-io) for the full scope-boundary guide (when to reach for `get_parameter`, `get_op_errors`, `get_dat_content`, etc. instead).

### Export

=== "Keyboard Shortcut"

    - ++ctrl+shift+e++ — Export entire project to `.tdn`
    - ++ctrl+alt+e++ — Export current COMP to `.tdn`

=== "MCP Tool"

    Use the `export_network` tool:

    - `root_path` — Starting COMP (default: `/` for entire project)
    - `include_dat_content` — Include DAT text/table content
    - `output_file` — File path to write (use `"auto"` for automatic naming)
    - `max_depth` — Maximum recursion depth
    - `embed_all` — Recurse into TDN-tagged COMPs instead of writing `tdn_ref` pointers, producing a self-contained export

### Import

Use the `import_network` MCP tool:

- `target_path` — Destination COMP path
- `tdn` — The TDN document (parsed object)
- `clear_first` — Delete existing children before importing

## TDN as Externalization Strategy

COMPs can use TDN as their externalization strategy (instead of `.tox`). With TDN strategy:

1. Press ++ctrl+shift+u++ to update — children are exported to `.tdn` files
2. On project save (++ctrl+s++), children are exported to `.tdn` — in the default **Export-on-Save** mode nothing is stripped from the `.toe`, so the `.toe` remains authoritative. In the experimental **Roundtrip** mode, children are also stripped from the `.toe` to keep it small, then restored after save completes.
3. In git, you see readable YAML diffs instead of binary changes

By default (Export-on-Save) the `.toe` is the source of truth on open, so COMPs are **not** rebuilt from `.tdn` — Embody only reconstructs a TDN COMP that is *absent* from the `.toe` (e.g. an agent built it and the `.toe` was never saved). In **Roundtrip** mode, every TDN COMP is reconstructed from its `.tdn` file on open.

This is configured per-COMP through the Embody externalization interface, and the mode is set via the `Tdnmode` parameter (Off / Export-on-Save / Roundtrip).
