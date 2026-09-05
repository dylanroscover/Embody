# TDXN Specification

**Version 2.0**

TDXN is the substrate that makes "create at the speed of thought" possible. It's the format your AI agent reads to understand what's on the screen, the format that lets you compare two attempts side by side, and the format a network rebuilds itself from on the next project open. Without it, AI-driven TouchDesigner work is one-directional — you generate, and you're stuck with what you got. With it, every step of the loop — generate, compare, revert, branch — runs at the speed of typing.

TDXN (TouchDesigner eXternal Network) is a YAML-based file format for representing TouchDesigner operator networks as human-readable, diffable text. It stores only non-default properties, keeping files minimal.

- File extension: `.tdxn`
- MIME type: `application/yaml`
- Encoding: UTF-8
- Schema: [`tdn.schema.yaml`](../tdn.schema.yaml) — validates the parsed structure.

---

## Document Structure

A `.tdxn` file is a YAML document with the following top-level fields:

```yaml
format: tdxn
version: '2.1'
build: 1
generator: Embody/6.0.4
td_build: '2025.32050'
source_file: MyProject.toe
exported_at: '2025-02-19T12:34:56Z'
network_path: /
type: containerCOMP
options:
  include_dat_content: true
  include_storage: true
type_defaults: { ... }
par_templates: { ... }
custom_pars: { ... }
parameters: { ... }
sequences: { ... }
flags: [ ... ]
color: [0.3, 0.5, 0.9]
tags: [tdn]
comment: Main UI container
storage: { ... }
operators: [ ... ]
annotations: [ ... ]
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `format` | string | Yes | `"tdxn"` as of Embody 6.1; `"tdn"` in files written by 6.0 and earlier, and by any older Embody that re-exports one. Both are permanently valid on read. |
| `version` | string | Yes | Format version. Currently `2.0`. See [Back-compatibility](#back-compatibility) for how older versions are read. |
| `build` | integer | No | Embody build number for the exported COMP. Incremented each time the network is saved via Embody. Useful for version tracking and git diffs. **Omitted entirely** when the COMP has no build tracking (an untracked or portable network — no externalizations-table row and no `Build` parameter). Older files may carry an explicit `build: null`; readers still accept it. |
| `generator` | string | Yes | Tool that produced the file (e.g., `"Embody/6.0.4"`). |
| `td_build` | string | Yes | TouchDesigner version and build number (e.g., `"2025.32050"`). |
| `source_file` | string | No | Basename of the `.toe` project file the COMP was exported from (e.g., `"MyProject.toe"`). Informational provenance only; not used on import. |
| `exported_at` | string | Yes | ISO 8601 UTC timestamp of export (e.g., `"2025-02-19T12:34:56Z"`). |
| `network_path` | string | Yes | The COMP path represented by this file (e.g., `"/"` for the entire project). On paste/import as a *new* COMP, its basename names the new COMP (e.g., `"/specimen_lab/noise_terrain"` -> `noise_terrain`), sanitized via `tdu.validName`, collisions uniquified. |
| `type` | string | No | TouchDesigner operator type of the target COMP (e.g., `"baseCOMP"`, `"containerCOMP"`, `"geometryCOMP"`). Added in v1.1. Makes the file self-describing for portable import into other projects. On import, a mismatch between this field and the destination COMP's type triggers a warning. |
| `options` | object | Yes | Export settings used when generating this file. |
| `options.include_dat_content` | boolean | Yes | Whether DAT text/table content was included in the export. |
| `options.include_storage` | boolean | No | Whether operator storage entries were included in the export. Absent means `true` (included). Added in v1.2. Can be toggled per-COMP via the `embed_storage_in_tdn` storage key. |
| `type_defaults` | object | No | Per-type shared properties (parameters, flags, size, color, tags). See [Type Defaults](#type-defaults). |
| `par_templates` | object | No | Reusable custom parameter page definitions. See [Parameter Templates](#parameter-templates). |
| `custom_pars` | object | No | Target COMP's own custom parameter definitions and values. Same format as operator-level [`custom_pars`](#custom-parameters). Only present if the target COMP has custom parameters. |
| `parameters` | object | No | Target COMP's own non-default built-in parameter values. Same format as operator-level [`parameters`](#built-in-parameters). Only present if the target COMP has non-default built-in parameters. |
| `sequences` | object | No | Target COMP's own built-in/custom parameter sequences with non-default block counts or values. Same format as operator-level [`sequences`](#built-in-parameter-sequences). Added in v1.3. |
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

```yaml
- name: noise1
  type: noiseTOP
  position: [200, -100]
  size: [300, 150]
  color: [0.2, 0.6, 0.9]
  comment: Primary noise source
  tags: [audio, generator]
  parameters: { ... }
  custom_pars: { ... }
  flags: [ ... ]
  storage: { ... }
  startup_storage: { ... }
  inputs: [ ... ]
  comp_inputs: [ ... ]
  dat_content: ...
  dat_content_format: text
  children: [ ... ]
  palette_clone: true
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
| `startup_storage` | object | No | **Import-only.** Restored via `storeStartupValue()` on import; TouchDesigner exposes no read-back accessor for the startup dictionary, so the exporter never writes this field. |
| `inputs` | array | No | Only if the operator has [operator-level connections](#operator-connections). |
| `comp_inputs` | array | No | Only if the operator has [COMP-level connections](#comp-connections). COMPs only. |
| `dat_content` | string or array | No | Only for DAT-family operators when `include_dat_content` is `true` (or the DAT is inside an `animationCOMP`). See [DAT Content](#dat-content). |
| `dat_content_format` | string | No | `"text"` or `"table"`. Present whenever `dat_content` is present. |
| `dat_read_only` | boolean | No | `true` for a DAT whose content is read-only (TD auto-generates it and rejects writes on import). Written in place of `dat_content`. See [DAT Content](#dat-content). |
| `children` | array | No | Only for COMPs with child operators (excluding palette clones). Contains nested operator objects. See [Children and Hierarchy](#children-and-hierarchy). |
| `annotations` | array | No | Only for COMPs with [annotations](#annotations). Contains annotation objects. |
| `palette_clone` | boolean | No | `true` if this COMP is cloned from the TouchDesigner palette (`/sys/`). When set, children are not exported (TD recreates them from the clone source). |
| `sequences` | object | No | Only if the operator has built-in parameter sequences with non-default block counts or values. See [Built-in Parameter Sequences](#built-in-parameter-sequences). *Added in v1.3.* |
| `tdn_ref` | string | No | Only for COMPs with their own TDXN externalization. Relative file path to the child's `.tdxn` file. Mutually exclusive with `children`. See [COMP References](#comp-references-tdn_ref). *Added in v1.2.* |
| `tox_ref` | string | No | Only for COMPs with their own TOX externalization. Relative file path to the child's `.tox` file. Mutually exclusive with `children`. See [TOX References](#tox-references-tox_ref). *Added in v1.4.* |

### Compact Formatting

Short numeric vectors (position, size, color — up to four elements) are written inline with YAML flow style; longer or non-numeric sequences fall to block style:

```yaml
position: [200, -100]
size: [300, 150]
color: [0.2, 0.6, 0.9]
tags:
  - audio
  - generator
inputs:
  - noise1
```

This keeps the most common short vectors compact while longer arrays stay readable line-by-line.

---

## Back-compatibility

### v2.1: widened definition fields

v2.1 changes no structure and adds no required field. It exists to mark one
thing: several custom-parameter definition fields that were always scalars can
now also be a **per-component array** (see
[Per-component definition fields](#per-component-definition-fields)) --
`default`, `min`, `max`, `clampMin`, `clampMax`, `normMin`, `normMax`,
`readOnly`, `password`, `styleCloneImmune`, `defaultExpr`, `defaultBindExpr`,
`defaultMode`, `help`, `enable` and `enableExpr`.

Purely *new* keys would not have needed a version bump -- readers are required
to ignore unknown fields. A widened **type** on an existing key is different: a
pre-2.1 reader evaluates `readOnly: [false, true]` as a truthy value and forces
the whole tuplet read-only, and drops `enable: [true, false]` entirely, both
silently. Since an array is only ever written when a tuplet's components
genuinely disagree, most files are unaffected -- but the bump makes an older
build log `TDXN file is v2.1, newer than this build; some content may not
import` rather than quietly reconstructing the wrong state. Older builds still
read v2.1 files; only mixed tuplets are misread.

v2.0 is the first YAML release of the format. Because YAML is a strict superset of JSON, importers read **both** new YAML files and legacy JSON `.tdn` (versions `1.x`) transparently:

- Importers parse **json-first**: a document beginning with `{` or `[` is read by the JSON parser (with any leading UTF-8 BOM and whitespace stripped first), and only otherwise by the YAML parser. Correctness is therefore independent of whether a fast YAML C library is present.
- Legacy `.tdn` were tab-indented JSON, which YAML forbids as indentation; the json-first path reads them losslessly.
- The v1.5 array-of-lines `dat_content` and the older newline-escaped string form both still import (see [DAT Content](#dat-content)).

Migration is **lazy**: an existing JSON `.tdn` is rewritten as YAML the next time Embody saves it. A v2.0 YAML file opened by a pre-2.0 Embody build (JSON-only reader) will not parse — new builds read old files, but old builds cannot read new ones.

**Hand-edit caveats** (do not apply to Embody-written files, which never emit these): a hand-written YAML float needing float typing must include a decimal point (`1.0e-07`, not `1e-07`); duplicate mapping keys are silently last-wins; the loader is YAML 1.1, so an unquoted `012` is octal `10`, `0x10` is `16`, `on`/`off`/`yes`/`no` are booleans and `~` is null -- quote such strings. Round-trip through Embody after manual edits.

---

## Built-in Parameters

The `parameters` object maps parameter names to their values. Only built-in (non-custom) parameters whose current value differs from their default are included. Parameters shared unanimously across all operators of a type are factored into [type_defaults](#type-defaults) instead.

### Parameter Modes

Parameters can be in one of three exportable modes:

**Constant** — the value is stored directly:
```yaml
parameters:
  tx: 100
  name: hello
  active: true
```

**Expression** — prefixed with `=`. A Python expression that TouchDesigner evaluates each frame:
```yaml
parameters:
  tx: =absTime.frame * 0.1
  resizecomp: =me
```

**Bind** — prefixed with `~`. A reference expression that binds this parameter to another:
```yaml
parameters:
  tx: "~op('controller').par.posx"
```

!!! note
    A fourth mode, **Export**, exists in TouchDesigner but is not stored in TDXN. Export mode is set by the exporting operator, not the parameter itself, and cannot be meaningfully imported.

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
    `file` and `syncfile` parameters ARE exported when non-default, so that TDXN files are self-contained for externalized DATs. This ensures file references survive import into a different project.

**By style:**

- `Pulse` — action buttons (fire-once, no persistent state)
- `Momentary` — momentary buttons (no persistent state)
- `Header` — visual section dividers (no value)

**Other exclusions:**

- Read-only parameters
- Custom parameters (handled separately in `custom_pars`)

### Per-Parameter Omission (`tdn_exclude:<par>` tags)

A project can exclude an individual parameter's **value** from export by tagging the operator itself with the suffixed form of the exclude tag:

```
tdn_exclude:<parname>
```

One tag per parameter (`tdn_exclude:file`, `tdn_exclude:Port`). The operator, its wiring, and its other parameters export normally; the named parameter's **constant value** is left out of the document. This is the opt-out for runtime state that must not be committed — a negotiated port, a session-specific file path, a live readout. (The **bare** tag on a COMP is the whole-COMP exclusion described elsewhere; the colon-suffixed form never affects COMP visibility.)

- **Constant values only.** An expression or bind on the tagged parameter still exports — a reference is authored configuration, not leaked session state.
- **Custom parameters**: the definition still ships (style, range, default, help); only its `value` key is dropped.
- **Visible by design**: the tag round-trips in the operator's `tags:` list, so the exported document records *why* the parameter is absent — and because the marker survives reconstruction, the next export omits it again.
- **Top-level parameters only**: sequence block parameters are not omittable.
- **Validation**: a tag naming a parameter the operator does not have logs a `WARNING` at export ("nothing omitted"), so a typo cannot silently no-op.
- The prefix is the same `Tdnexcludetag` parameter that governs whole-COMP exclusion (default `tdn_exclude`); clearing it disables the whole family.

The Embody UI exposes this through the tagger's **Exclude from tdn** action on TDXN COMPs — a drop-zone panel that toggles `tdn_exclude:<par>` for dragged parameters (and the bare whole-COMP `tdn_exclude` for dragged COMPs), listing every exclusion in the COMP's subtree, each removable via its **×**.

### Non-Default Comparison

A constant parameter is included only if its current value differs from its default:

- **Floats**: considered different if `abs(current - default) > 1e-9`
- **OP-reference parameters**: `None` and `""` are treated as equivalent (both mean "no operator connected")
- **All other types**: standard equality comparison (`!=`)

### Divergent Defaults and the Creation-Defaults Catalog

Some TouchDesigner operators reset certain parameters during initialization, meaning the value reported by `p.default` differs from the value TouchDesigner actually assigns when the operator is created. For example:

- `cameraCOMP` `tz`: `p.default` reports `0`, but TD creates cameras with `tz = 5`
- `lightCOMP` `tz`: `p.default` reports `0`, but TD creates lights with `tz = 5`
- `renderTOP` resolution parameters: reported defaults differ from creation values

If TDXN used `p.default` directly for non-default comparison, a user-set value that happens to match `p.default` but differs from the actual creation value would be silently omitted from export. On import, TD would create the operator with the (different) creation value, and the user's intended value would be lost. Conversely, a parameter at the true creation value might be incorrectly included in the export, bloating the file with false positives.

#### The Catalog System

Embody solves this with a three-tier system that discovers and caches the true creation values for every operator type:

**1. Background scan at startup**

On project open, the `CatalogManager` extension checks whether a creation-values catalog exists for the current TD build (stored as a JSON file in the `.embody/` directory at the project root). If no catalog exists, it runs a background scan:

- Iterates every creatable operator type in TouchDesigner (TOPs, CHOPs, SOPs, DATs, MATs, COMPs, POPs)
- Creates a temporary instance of each type
- Records `p.val` (the actual value TD assigned) for every parameter, skipping custom, read-only, and sequence parameters, a small set of known non-portable parameter names and styles, and name-dependent values (e.g. callback-DAT paths that embed the probe operator's name)

The catalog stores **all** creation values, not just divergent ones. This means TDXN always has the ground-truth creation value for every parameter — `_getCreationDefault()` returns the catalog value directly, falling back to `p.default` only for parameters the catalog doesn't cover. The divergent-default problem (where `p.val` differs from `p.default`) is solved implicitly: by recording what TD actually assigns, the catalog captures both correct and divergent defaults without needing to distinguish between them.

The scan processes 1–2 operator types per frame to avoid dropping frames. The resulting catalog is written to `.embody/catalog_{build}.json` (e.g., `.embody/catalog_099.2025.32280.json`) and cached for future sessions on the same TD build.

**2. Export/import correction**

During TDXN export, the `_getCreationDefault()` method checks the catalog before falling back to `p.default`:

```python
def _getCreationDefault(self, op_type, par_name, par):
    divergent = self._getDivergentDefaults(op_type)
    if par_name in divergent:
        return divergent[par_name]
    return par.default
```

This means TDXN compares parameter values against the *actual creation value*, not the *reported default*. Parameters are included in the export if and only if the user's value truly differs from what TD would assign to a freshly created operator.

**3. Cross-build patching**

When a `.tdxn` file is opened on a different TD build than the one that exported it, creation defaults may have shifted between builds. The `CatalogManager` handles this automatically:

1. Reads the `td_build` field from each `.tdxn` file header
2. Loads the catalog for the source build (if available)
3. Loads the catalog for the current build
4. Finds parameters whose creation defaults changed between the two builds
5. For each affected operator, checks if the current value matches the *new* default — if so, the parameter was omitted during export (because it matched the *old* default), and TD has now assigned the wrong new default
6. Patches those parameters back to the old creation value

A summary dialog is shown to the user listing every corrected parameter.

#### Fallback chain

The catalog is loaded in priority order:

1. **`.embody/` catalog file** — per-build JSON written by the background scan
2. **Embedded `divergent_defaults` table** — a DAT inside the Embody COMP with bootstrap data for known TD builds
3. **On-the-fly probing** — if neither source has data for the current build, a temporary operator is created at export time to discover the true creation value

This ensures correct non-default comparison regardless of TD version, even on builds that Embody has never seen before.

---

## Built-in Parameter Sequences

*Added in v1.3.*

Many TouchDesigner operators have **resizable parameter blocks** — mathmixPOP Combine blocks, glslPOP/glslTOP uniform sequences, attributePOP attribute blocks, constantCHOP channel blocks, etc. These are called **parameter sequences** in TD's API.

The `sequences` object stores per-operator sequence data. It is keyed by sequence name, where each value is an array of block objects containing only non-default parameter values using **base names** (without the sequence prefix or block index):

```yaml
- name: mathmix1
  type: mathmixPOP
  sequences:
    comb:
    - {oper: A, scopea: P, result: startPos}
    - {oper: A + B, scopea: vel, scopeb: direction, result: vel}
    - {}
```

### Design

- **Array length = `numBlocks`**: The importer sets `seq.numBlocks = len(blocks)` to create the right number of parameter slots before setting values.
- **Base names**: `"oper"` rather than `"comb0oper"`. The full parameter name is `{seqName}{blockIndex}{baseName}` (e.g., `comb2oper`), but only the base name is stored. This makes the format portable and readable.
- **Empty objects `{}`**: Represent blocks where all parameters are at their default values. Included to preserve correct block count and ordering.
- **Omission**: The `sequences` key is omitted entirely when all sequences on the operator have their default block count and all block values are defaults.
- **Default block count**: probed from a throwaway instance of the operator type (once per type per session), never assumed to be `1`. A sequence sitting *below* its type-default count therefore still exports its all-default block list, so the shrink survives reimport.
- **Value shorthand**: Same as built-in parameters — `=` prefix for expressions, `~` prefix for binds, literal values for constants.

### Import Phase

Sequences are expanded in two passes:

- **Phase 2.5** (before Phase 3 parameter setting): sequences whose blocks carry values. This ensures the dynamically-created sequence parameters exist before Phase 3 attempts to set values on them.
- **Phase 5.5** (after Phase 5 wiring): sequences whose blocks are **all empty** (`[{}, {}, ...]`). An all-empty list is purely a `numBlocks` instruction, and some sequences track the operator's wired inputs (a `mergePOP`'s `input` sequence grows one block per connection). Applying such a count before wiring declares inputs on an unwired operator and latches a `No input POP`-class error that surfaces as a reconstruction error on project open. Applied after wiring, the count already matches and the set is a no-op — while independent sequences still receive their count correctly.

### Exclusion from type_defaults

Sequence data is **never included in `type_defaults`**. Sequences are inherently per-instance (different operators have different block counts), so they cannot be compressed into per-type defaults.

---

## Custom Parameters

The `custom_pars` object maps page names to arrays of parameter definitions. Unlike built-in parameters, custom parameters are **always fully exported** (including their definitions, ranges, and current values) because the importer must recreate them from scratch.

!!! note
    Only COMPs can have custom parameters in TouchDesigner.

!!! note "Transient status parameters (Embody-managed COMPs)"
    Parameters registered as runtime status readouts on Embody-managed
    COMPs (registry: `EmbodyExt._TRANSIENT_STATUS_PARS`, scoped by the
    COMP's global OP shortcut) are exported with their declared **resting
    value** instead of the session value -- a status like
    `Running on port 9872` is machine state, not authored config, and
    would otherwise churn in version control on every export. Companion
    registry `_TDN_VALUE_OMIT_PARS` drops the `value` key of
    machine-written metadata stamps (`Build`, `Date`; definitions still
    ship). `_TRANSIENT_STATUS_PARS` also holds a few **user preferences**
    whose live value is restored per machine from `.embody/config.json`
    rather than from the file -- `Convoyenable`, `Clipboardautopaste`,
    `Filecleanup`, `Toxdropexpr` -- exported with their resting value
    (`keep`, `ask`, ...) like any other registered status par, so a receipt
    or a released `.tox` never carries one developer's (or the test
    runner's) setting into every project. Expression
    and bind values (the `=`/`~` shorthand) are never replaced. A
    registered parameter **sequence** exports no block values; its block
    count ships only when it differs from the type default. User COMPs
    are never affected -- the registries key on global OP shortcuts.

    One page is dropped outright rather than value-stripped: an `About`
    page whose parameters are **only** Embody's per-COMP metadata stamp
    (`Build`, `Date`, `Touchbuild`) is omitted from `custom_pars`
    entirely, because those values are reconstructed from
    `externalizations.tsv` on import. The stamp is written on every
    externalized COMP, so most COMPs' `.tdxn` files carry no About page
    at all. An About page holding any additional authored parameter --
    including the Embody COMP's own, larger About page -- is exported
    normally.

### Page-Grouped Format

Custom parameters are grouped by page name. Each page contains an array of parameter definitions:

```yaml
custom_pars:
  Controls:
  - name: Speed
    style: Float
    default: 1
    max: 10
    clampMin: true
    normMax: 5
    value: 2.5
  - name: Mode
    style: Menu
    menuNames: [linear, ease, bounce]
    menuLabels: [Linear, Ease In/Out, Bounce]
    value: 1
  About:
  - name: Build
    style: Int
    label: Build Number
    readOnly: true
    value: 14
```

The page name is the dict key — individual parameter definitions do not include a `"page"` field.

### Template References

When a page's parameter definitions match a [parameter template](#parameter-templates), the page is stored as a template reference with value overrides:

```yaml
custom_pars:
  About:
    $t: about
    Build: 14
    Date: 2026-02-19 16:09:43 UTC
    Touchbuild: '2025.32050'
```

The `$t` field names the template. Other keys are parameter value overrides (parameter name → current value). See [Parameter Templates](#parameter-templates).

### Custom Parameter Definition

| Field | Type | Condition | Description |
|-------|------|-----------|-------------|
| `name` | string | Always | Base name of the parameter. For multi-component parameters, this is the group name without any suffix (e.g., `"Pos"` for a group of `Posx`, `Posy`, `Posz`). |
| `label` | string | If different from `name` | Display label shown in the parameter dialog. Omitted when the label matches the parameter name. |
| `style` | string | Always | Parameter style. See [Supported Styles](#supported-styles). |
| `size` | integer | Multi-component `Float`/`Int`; suffix-style groups with non-full arity | Number of components when > 1 (e.g., `3` for a 3-component float). Also written for suffix-style groups (`RGBA`, `XYZW`, `UVW`) whose component count differs from the style's full size — TouchDesigner reports style `RGBA` for both RGB (3) and RGBA (4) groups, `XYZW` for XY/XYZ/XYZW, and `UVW` for both UV (2) and UVW (3), so `size` disambiguates (e.g. `3` for an RGB group, `2` for a UV group). When omitted on a suffix-style group, importers use the `values` length, else the style's full component count. |
| `default` | any or array | If non-standard | Default value. Omitted when every component's default is a standard value (`0`, `0.0`, `""`, or `false`). Per-component: a scalar when the whole tuplet agrees, an array (one entry per component) when it does not. See [Per-component definition fields](#per-component-definition-fields). |
| `min` | number or array | If != `0` | Minimum value. Per-component (see below). |
| `max` | number or array | If != `1` | Maximum value. Per-component (see below). |
| `clampMin` | boolean or array | If `true` | Whether the value is clamped to `min`. Per-component (see below). |
| `clampMax` | boolean or array | If `true` | Whether the value is clamped to `max`. Per-component (see below). |
| `normMin` | number or array | If != `0` | Normalized range minimum (for slider UI). Per-component (see below). |
| `normMax` | number or array | If != `1` | Normalized range maximum (for slider UI). Per-component (see below). |
| `menuNames` | array of strings | Manually defined menus | Internal names for each menu option. |
| `menuLabels` | array of strings | If different from `menuNames` | Display labels for each menu option. Omitted when labels match names. |
| `menuSource` | string | Dynamically populated menus | DAT path or expression that populates the menu. When present, `menuNames`/`menuLabels` are omitted. |
| `startSection` | boolean | If `true` | Whether this parameter starts a new visual section. |
| `readOnly` | boolean or array | If `true` | Whether the parameter is read-only. Per-component (see below). |
| `password` | boolean or array | If `true` | Masks the value as asterisks in the parameter dialog. TouchDesigner allows it on custom `Str`/`Int`/`Float` parameters. Per-component. |
| `styleCloneImmune` | boolean or array | If `true` | Keeps the parameter *definition* from being matched to the master on a clone sync. Per-component. |
| `bindRange` | boolean | If `true` | Routes `min`/`max`/`clampMin`/`clampMax`/`normMin`/`normMax`/`normVal` to the bind master. **Tuplet-wide**, not per-component — TouchDesigner propagates it across every component of a group, so it always serializes as a scalar (verified 2026-09-04). |
| `defaultExpr` | string or array | If non-empty | The parameter's default *expression*, distinct from the constant `default` and from the parameter's live `value`. Stored raw — the `=`/`~` shorthand applies to `value`/`values` only. Per-component. |
| `defaultBindExpr` | string or array | If non-empty | The parameter's default bind expression. Stored raw, same as `defaultExpr`. Per-component. |
| `defaultMode` | string or array | If not `CONSTANT`, or whenever `defaultExpr`/`defaultBindExpr` is set | `ParMode` name (`CONSTANT`, `EXPRESSION`, `EXPORT`, `BIND` -- all four, verified 2026-09-04) the parameter resets to. Written even when `CONSTANT` if a default expression is present: assigning `defaultExpr` auto-flips `defaultMode` to `EXPRESSION`, so a parameter authored with a default expression but forced back to `CONSTANT` would otherwise reconstruct with the wrong reset behavior (verified 2026-09-04). On import it is applied **after** the expressions, to override that side effect. Per-component. |
| `enable` | boolean or array | If `false` | Static enable state (greyed out when `false`). Omitted when an `enableExpr` is set. Per-component. |
| `enableExpr` | string or array | If set | Python expression that controls the enable state (conditional greying). Per-component in TDXN, though TouchDesigner documents it as shared across a ParGroup. |
| `sequence` | string | Template pars of a custom sequence | Name of the custom sequence this definition belongs to. |
| `help` | string or array | If non-empty | Tooltip help text shown when hovering the parameter in the dialog. Omitted when empty. Per-component. |
| `value` | any | Single-component, if non-default | Current value. Can be a constant, `"=expr"` string, or `"~bind"` string. Omitted when the value equals the default. |
| `values` | array | Multi-component, if any non-default | Current values for each component. Same format as `value` per element. Omitted when all values equal their defaults. |

#### Per-component definition fields

`default`, `min`, `max`, `clampMin`, `clampMax`, `normMin`, `normMax`, `readOnly`,
`password`, `styleCloneImmune`, `defaultExpr`, `defaultBindExpr`, `defaultMode`,
`help`, `enable` and `enableExpr` belong to each *component* of a tuplet, not to
the group: an `RGBA` parameter has four independent defaults, a `WH` two
independent norm ranges, and a tuplet can be read-only in one component only.

`bindRange` is the documented exception — TouchDesigner propagates it across the
whole tuplet, so it always serializes as a scalar.

- Written as a **scalar** when every component holds the same value (the common
  case, and what single-component parameters always produce).
- Written as an **array**, one entry per component in group order, when the
  components differ.

```yaml
- name: Color
  style: RGBA
  default: [0.1, 0.2, 0.3, 1]     # per component
- name: Size
  style: WH
  default: [500, 250]
  normMax: [1000, 2000]
- name: Pos
  style: XYZ
  default: 2.5                    # all three components agree
```

Readers broadcast a scalar across every component and map an array 1:1 (a short
array leaves the remaining components untouched). Files written before Embody
6.2.11 carry only the first component's value as a scalar; reading them
broadcasts that value, which is why a vector parameter's defaults can shift
once on the first re-read of an old file.

### Supported Styles

All 32 parameter styles recognized by TDXN:

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

```yaml
- name: Pos
  style: XYZ
  values: [10.0, 20.0, 30.0]
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

```yaml
- name: Weight
  style: Float
  size: 3
  values: [0.5, 0.3, 0.2]
```

This creates three parameters: `Weight1`, `Weight2`, `Weight3`.

---

## Type Defaults

The `type_defaults` section hoists properties that are shared unanimously across **all** operators of a given type into a single location, removing them from individual operators. Supported properties: `parameters`, `flags`, `size`, `color`, and `tags`.

```yaml
type_defaults:
  containerCOMP:
    parameters:
      borderover: false
      reloadbuiltin: false
      resizecomp: =me
      repocomp: =me
    flags:
    - viewer
    size: [300, 150]
  textDAT:
    parameters:
      language: text
    flags:
    - viewer
    size: [130, 90]
    color: [0.67, 0.67, 0.67]
    tags:
    - source
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

```yaml
par_templates:
  about:
  - {name: Build, style: Int, label: Build Number, readOnly: true}
  - {name: Date, style: Str, label: Build Date, readOnly: true}
  - {name: Touchbuild, style: Str, label: Touch Build, readOnly: true}
```

Templates contain parameter definitions **without values** — they define the structure (name, style, label, ranges, etc.) of a page's parameters.

### Template References

Operators reference templates via `$t` in their `custom_pars`:

```yaml
custom_pars:
  About:
    $t: about
    Build: 14
    Date: 2026-02-19 16:09:43 UTC
    Touchbuild: '2025.32050'
```

| Field | Description |
|-------|-------------|
| `$t` | Template name (matches a key in `par_templates`) |
| Other keys | Value overrides: parameter name → current value |

### Import Behavior

On import, `$t` references are resolved before Phase 2 (create custom parameters). Each template reference is expanded back into a full array of parameter definitions, with value overrides applied:

```yaml
# Resolved from template + overrides:
About:
- {name: Build, style: Int, label: Build Number, readOnly: true, value: 14}
- {name: Date, style: Str, label: Build Date, readOnly: true, value: 2026-02-19 16:09:43 UTC}
- {name: Touchbuild, style: Str, label: Touch Build, readOnly: true, value: '2025.32050'}
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
| `allowCooking` | `true` | Whether the COMP is allowed to cook. Readable on any operator, but **only a COMP can disable it**. |
| `cloneImmune` | `false` | The operator is immune to clone re-sync. Present on every operator, not COMPs only (verified 2026-09-04). |
| `componentCloneImmune` | `false` | The "including children" state of the Immune flag: the COMP **and**, recursively, every node inside it are immune to clone re-sync. Paired with `cloneImmune` (which covers the node alone) as the Python half of one three-state UI flag. **COMPs only** (the attribute does not exist on other operators). |
| `showCustomOnly` | `false` | The parameter dialog shows only custom pages. Present on every operator. |
| `showDocked` | `true` | Docked operators are shown attached to their host. Present on every operator; being `true` by default, it serializes as `-showDocked` when turned off. |

An importer ignores any flag name outside this table (it never assigns arbitrary attributes from a file), and additionally skips any flag the destination operator cannot carry — `componentCloneImmune` on a TOP, or `allowCooking` on anything that is not a COMP.

!!! note "Defaults are per-type creation values"
    The exporter compares each flag against the **creation defaults probed for that operator type** — the same catalog-probe mechanism used for [parameter creation defaults](#divergent-defaults-and-the-creation-defaults-catalog): a temporary operator of the type is created and its actual flag values recorded. The table above is the common fallback baseline, used when probing is unavailable, not a guarantee for every operator type. For a type whose creation default differs from the listed value, an omitted flag means "this type's creation default", not the value in the table.

### Format

Flags that default to `false` are listed by name when set to `true`:
```yaml
flags:
- viewer
- display
```

Flags that default to `true` use a `-` prefix when set to `false`:
```yaml
flags:
- -expose
```

Combined example — viewer on, cooking disabled:
```yaml
flags:
- viewer
- -allowCooking
```

### Lock Flag Limitation

!!! warning "Locked content is NOT preserved for TOPs, CHOPs, or SOPs"

    TDXN preserves the **lock flag** for all operator families, but it **cannot store frozen pixel, channel, or geometry data**. After a TDXN round-trip (export + import), locked non-DAT operators will be locked but **empty** — no texture, no samples, no mesh.

    **This is by design, not a bug.** Storing binary data would defeat TDXN's purpose as a diffable, version-control-friendly format. A single locked 4K TOP could add over 100 MB to a `.tdxn` file.

    Embody warns you at save time if your network contains locked non-DAT operators. The warning covers only operators the export itself serializes — locked content inside a nested tox/tdn-tagged child COMP (exported separately as its own boundary) or an exclude-tagged subtree does not trigger it.

The `lock` flag applies to **all** operator families — DATs, TOPs, CHOPs, and SOPs — freezing their cooked output so it no longer updates from inputs or parameters. However, TDXN only persists the frozen data for DATs.

| Family | Flag persisted? | Frozen data persisted? | Notes |
|--------|:-:|:-:|---|
| **DAT** | Yes | Yes (via `dat_content`) | Full round-trip: both the lock state and text/table content are preserved. |
| **TOP** | Yes | **No** | Pixel data is not stored. On import, the lock flag is set but no texture data exists. The operator will appear black. |
| **CHOP** | Yes | **No** | Channel data is not stored. On import, the lock flag is set but no sample data exists. |
| **SOP** | Yes | **No** | Geometry data is not stored. On import, the lock flag is set but no mesh data exists. |

**Workarounds:**

- **Unlock before saving** — the operator will re-cook from its inputs on reload.
- **Use TOX strategy** instead of TDXN for COMPs containing locked non-DAT operators. TOX files are binary and preserve all locked content.
- **Store data externally** — write pixel data to image files, channel data to CSV, etc., and reference them from your network.

---

## Connections

TouchDesigner operators have two kinds of connections. TDXN stores both as string arrays where array position equals the input index.

### Operator Connections

Standard wiring between operators (left/right connectors). Stored in the `inputs` array:

```yaml
inputs:
- noise1
```

Multi-input example — `noise1` at index 0, nothing at index 1, `level1` at index 2:
```yaml
inputs:
- noise1
- null
- level1
```

### COMP Connections

COMP-level wiring (top/bottom connectors). Only applicable to COMPs. Stored in the `comp_inputs` array:

```yaml
comp_inputs:
- container1
```

### Source Resolution

Each string element references the source operator:

- If the source operator is a **sibling** (same parent), only the operator **name** is stored (e.g., `"noise1"`).
- If the source is in a **different parent**, the full **path** is stored (e.g., `"/project/other/transform1"`).
- `null` means no connection at that index.

On import, the source is resolved by first looking for a sibling with that name, then falling back to interpreting it as a full path.

### Array Length

The array is truncated at the **last connected index** — trailing `null`
entries never appear. Dynamic multi-input operators (Switch, Composite,
Merge) always expose one trailing empty connector in TouchDesigner; it is
never serialized, so densely wired networks produce byte-identical
`inputs` arrays across exports. An exporter that emitted trailing nulls
would produce a spurious diff on every multi-input operator in every
file.

Indices are derived from the operator's real **input connectors**, never
from TouchDesigner's `OP.inputs` (a compacted list of connected sources
that cannot represent a gap) — a wire on connector 2 with connectors 0-1
empty exports as `[null, null, "src"]`, and reimports onto connector 2.

### Known Limitation: Source Output Index

Each element names the source operator only; there is no field for the
source's **output connector index**. A multi-output source (e.g. a COMP
with several Out operators) wired from output 1 or higher is re-imported
from output 0. Representing it would require a format extension (the
importer already tolerates dict-shaped entries for forward
compatibility).

---

## Docking

Operators in TouchDesigner can be visually docked to other operators. A docked operator moves with its host in the network editor and can be collapsed into the host's tile.

When an operator is docked, TDXN records a `"dock"` field on it:

| Field | Type | Condition |
|-------|------|-----------|
| `dock` | string | Only if the operator is docked to another operator. |

The value is the **sibling name** of the dock host when they share a parent COMP, or the **full operator path** for cross-hierarchy docking. This follows the same reference convention as [operator connections](#connections).

Docking is a purely visual/organizational relationship — it has no effect on operator behavior, data flow, or cooking. It is omitted from `type_defaults` because docking is always instance-specific.

**Example:**

```yaml
- name: info1
  type: infoDAT
  dock: noise1
```

During import, the dock target is resolved by sibling name first, then full path fallback. If the target cannot be found, a warning is logged and docking is skipped gracefully.

---

## DAT Content

DAT-family operators can optionally include their text or table data. This is controlled by the `include_dat_content` option.

### Text Format

For text-based DATs (textDAT, etc.):

```yaml
- name: script1
  type: textDAT
  dat_content: |
    print('hello world')
    print('goodbye')
  dat_content_format: text
```

- `dat_content`: the DAT's text, stored in v2.0 as a **plain string**. Multi-line scripts are rendered on disk as a YAML **literal block scalar** (`|`), so the text stays human-readable and git diffs it line-by-line. Single-line content stays a compact inline string.
- `dat_content_format`: `text`

**Trailing-newline chomping.** YAML's literal block scalar preserves the exact number of trailing newlines through a chomping indicator chosen automatically by the writer: `|-` (strip) when the text has no trailing newline, `|` (clip) for a single trailing newline, and `|+` (keep) for two or more. The round-trip is therefore byte-exact for trailing whitespace.

**Back-compatibility:** v1.5 stored a multi-line text DAT's `dat_content` as an **array of line-strings**, and v1.x earlier stored it as a single newline-escaped string. Both still import unchanged — a string is used as-is, an array is joined with `\n`. Examples of the still-valid legacy forms:

```json
{
  "name": "script1",
  "type": "textDAT",
  "dat_content": ["print('hello world')", "print('goodbye')"],
  "dat_content_format": "text"
}
```

```json
{
  "name": "script1",
  "type": "textDAT",
  "dat_content": "print('hello world')\nprint('goodbye')",
  "dat_content_format": "text"
}
```

### Table Format

For table-based DATs (tableDAT, etc.):

```yaml
- name: lookup1
  type: tableDAT
  dat_content:
    - [name, value, type]
    - [speed, '1.5', float]
    - [active, '1', int]
  dat_content_format: table
```

- `dat_content`: array of row arrays (each row is an array of cell value strings)
- `dat_content_format`: `table`

### Inclusion Conditions

DAT content is only included when:

1. The operator belongs to the DAT family
2. The `include_dat_content` option is `true`, **OR** the DAT lives inside an `animationCOMP` (its keys/channels/graph/attributes tableDATs hold all keyframe data and are always saved regardless of the option)
3. The DAT has content (non-empty text or at least one row)

**Read-only DATs.** When a DAT's content is read-only (e.g. `glsl1_info`, `popto1` — TD auto-generates their content and rejects writes on import), its text is **not** serialized. Instead the operator carries `dat_read_only: true` so the importer knows to skip content restoration for it.

### Boilerplate Omission

Auto-created default docked compute DATs (the "Example Compute Shader" companion that TouchDesigner spawns alongside a `glslTOP` / `glslmultiTOP`) are **not serialized** when their text still matches TD's default template. TD recreates the exact default on import, so omitting it keeps the file smaller with no loss. A compute DAT whose shader has been edited away from the default is exported normally.

---

## Operator Storage

Every TouchDesigner operator has a `.storage` dictionary for persistent Python data. TDXN exports all serializable storage entries except known transient/internal keys used by Embody's runtime. Keys are written in sorted order. A non-finite float (`nan`, `inf`) is skipped with a debug log, never aborting the export.

!!! warning "Reserved keys"
    A two-key mapping of exactly `$type` and `$value` is the wrapper the format uses for tuples, sets and bytes (see [Value Serialization](#value-serialization)); a user dictionary of that exact shape is reconstructed as the wrapped type on import. Any other dictionary containing `$type` is a plain dictionary.

### Per-COMP Storage Toggle

Storage export can be disabled per-COMP by setting the `embed_storage_in_tdn` storage key to `false` on the target COMP, or globally via Embody's `Embedstorageintdns` parameter. When disabled, the `options.include_storage` field is `false` and operator storage entries are omitted — except for Embody control keys (`embed_dats_in_tdn`, `embed_storage_in_tdn`) which are always preserved to maintain round-trip fidelity of export preferences.

### Format

```yaml
- name: my_comp
  type: baseCOMP
  storage:
    count: 42
    label: hello
    items: [1, 2, 3]
    config: {key: value}
    coords: {$type: tuple, $value: [10, 20]}
    tags: {$type: set, $value: [a, b, c]}
    raw: {$type: bytes, $value: AAEC/w==}
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

The following storage keys are never exported — runtime and session state managed by Embody, grouped by what each family holds:

| Category | Keys |
|----------|------|
| Embody runtime / UI state | `_git_root`, `envoy_running`, `envoy_shutdown_event`, `claudius_running`, `expanded_paths`, `expand_order`, `git_status`, `manage_file_path`, `visible_count`, `hover`, `pressed`, `_tip_trace` |
| Dirty-detection baselines | `_tdn_fingerprints` |
| Strip and restore markers | `_tdn_stripped_paths`, `_tdn_rel_path`, `_pending_tox_restore`, `_pending_tdn_restore` |
| Lifecycle flags | `_suppress_dialogs`, `_init_complete`, `_start_in_progress`, `_release_hook_active`, `_tdn_restore_failures` |
| Test-runner bookkeeping | `_test_saved_filecleanup`, `_test_saved_toxdropexpr`, `_test_saved_status`, `_smoke_test_responses`, `test_results`, `cp_summary`, `_test_run_active`, `_test_run_owner` |
| Loop generation counters | `_watchdog_gen`, `_clip_watch_gen`, `_shortcut_rec_gen`, `_convoy_gen` |

Two families are worth understanding rather than just listing. The **restore markers** are per-`.toe` recovery state: a serialized `_tdn_rel_path` would make every pasted copy claim the original's file, and a serialized `_pending_tdn_restore` would be replayed by Phase 6a on every import, re-importing the child from a baked ref path forever. The **generation counters** are bumped on every extension reinit purely so a previous instance's pending `run()` tick retires itself; because they change constantly, exporting them rewrote several lines of committed `.tdxn` files on every save.

### Startup Storage

TouchDesigner supports `storeStartupValue(key, value)` — values that reset to their initial state on every project open, regardless of what they were when the file was saved. TDXN supports this via an optional `startup_storage` field:

```yaml
- name: my_comp
  type: baseCOMP
  storage: {runtime_count: 0}
  startup_storage: {version: 1, default_mode: auto}
```

On import, keys in `startup_storage` are restored via `storeStartupValue()`, while keys in `storage` use `store()`.

**Export limitation:** TouchDesigner provides no API to introspect which storage keys were set with `storeStartupValue()` vs `store()`. During automatic export, all entries go into `storage`. The `startup_storage` field must be populated manually or by tools that know the intent (e.g., code generators, StorageManager-aware exporters).

### Import Behavior

Storage is restored during Phase 6a (after DAT content, before positions). Keys in `storage` are restored via `op.store(key, value)`. Keys in `startup_storage` are restored via `op.storeStartupValue(key, value)`. `$type` wrappers are deserialized back to their Python types. Unknown `$type` values are treated as plain dicts with a warning logged.

The [skip list](#skipped-keys) is applied on import as well: a skipped key found in a `.tdxn` is ignored rather than restored. A stale entry written by an older build therefore dies on the next round-trip instead of ratcheting forward forever.

---

## Children and Hierarchy

COMPs can contain child operators. These are stored in the `children` array, which contains nested operator objects following the exact same schema:

```yaml
- name: container1
  type: baseCOMP
  children:
  - name: noise1
    type: noiseTOP
  - name: null1
    type: nullTOP
    position: [300, 0]
    inputs:
    - noise1
```

Note that `container1` omits `position` (defaults to `[0, 0]`) and `noise1` also omits `position`. Only `null1` at `[300, 0]` includes its position.

Nesting is recursive — COMPs inside COMPs can have their own `children`. The optional `max_depth` export parameter limits recursion depth (`null` means unlimited). COMPs may also contain an `annotations` array alongside `children` — see [Annotations](#annotations).

### Nested TDXN-Externalized COMPs

When a parent TDXN file contains `children` for a child COMP that has its **own** TDXN externalization entry in the externalizations table, the child's `children` array is **skipped during import**. The child COMP shell is still created (its operator definition — name, type, position, parameters — is applied), but its internal network is **not** populated from the parent's snapshot. The child's own `.tdxn` file is the source of truth for its contents.

This prevents a common problem: if a child COMP is updated and re-exported to its own `.tdxn` file, but the parent is not re-exported, importing the parent would silently overwrite the child with stale data. By skipping nested TDXN children, each `.tdxn` file owns exactly one level of the hierarchy.

**What this means in practice:**

- **Export** is unchanged — parent TDXN files still include the full recursive hierarchy in their `children` arrays. This keeps the file self-contained and useful as a portable snapshot.
- **Import** detects child COMPs with their own TDXN entries and skips their children. A log message is emitted for each skipped child (e.g., `Skipping children of /project/parent/child — has its own TDXN externalization`).
- **Reconstruction on project open** imports parents before children (sorted by path depth). Combined with the skip logic, this means each COMP's network is populated exactly once, from its own authoritative `.tdxn` file.

If a child COMP is removed from the externalizations table (no longer tagged for TDXN), its `children` array in the parent TDXN will be imported normally — no special handling needed.

### COMP References (`tdn_ref`)

*Added in TDXN v1.2.*

When a parent COMP is exported and a child COMP has its own TDXN externalization, the parent's operator definition for that child includes a `tdn_ref` field instead of a `children` array:

```yaml
- name: audio_mixer
  type: baseCOMP
  tdn_ref: Embody/project1/audio_mixer.tdxn
  position: [600, 0]
```

| Field | Type | Description |
|---|---|---|
| `tdn_ref` | `string` | Relative file path from the externalization folder to the child's `.tdxn` file. Includes the COMP name in the path for cross-validation. |

**Mutually exclusive with `children`**: When `tdn_ref` is present, the operator definition does not contain a `children` array. The COMP's internal network is defined entirely in the referenced file.

**Resolution**: On import, the importer creates the COMP shell (name, type, position, parameters, flags) and marks it with a `_pending_tdn_restore` storage key holding the ref path. [Phase 8.6](#import-process) then imports the referenced `.tdxn` into that shell **in the same import**, re-entering the importer so deeper nesting recurses naturally; an ancestor-chain guard refuses a true ref cycle (`A.tdxn` -> `B.tdxn` -> `A.tdxn`) while two sibling shells pointing at the same file both fill. A nested externalized COMP is therefore never left empty by an import — an empty shell reads as changed content and the next automatic export would overwrite the child's own good `.tdxn`.

Two callers pass `restore_tdn_shells=False` and skip Phase 8.6: **startup reconstruction** (`ext.Embody.reconstructTDNComps`) and the **post-save restore**. Their own depth-sorted loops already import every tracked TDXN COMP exactly once, parents before children, so filling shells inline would import the same files twice. In that mode the markers are only cleared, never acted on.

**Cross-validation**: The `tdn_ref` value is checked against two independent sources:

1. **Externalizations table**: The child COMP's path must have an entry with `strategy='tdn'` and a matching `rel_file_path`.
2. **Disk**: The referenced `.tdxn` file must exist at the resolved absolute path.

Mismatches produce warnings, not errors — the COMP shell is always created regardless. This ensures graceful degradation when files are moved or the table is out of sync.

**Backward compatibility**:

- Files **without** `tdn_ref` (TDXN v1.1 and earlier) continue to work. The existing `_stripNestedTDNChildren` mechanism handles them via the externalizations table.
- Files **with** `tdn_ref` imported by an older Embody that doesn't recognize the field will silently ignore it. The `_stripNestedTDNChildren` path handles the nested COMP correctly as a fallback.
- The `embed_all=True` export option suppresses `tdn_ref` and inlines all children, producing a fully self-contained file regardless of child externalization status.

### TOX References (`tox_ref`)

*Added in TDXN v1.4.*

The same ownership principle applies when a child COMP is externalized as `.tox` instead of `.tdxn`. The parent's operator definition for that child includes a `tox_ref` field instead of a `children` array:

```yaml
- name: wave_speed
  type: sliderCOMP
  tags:
  - tox
  tox_ref: Embody/project1/wave_speed.tox
  position: [475, 425]
```

| Field | Type | Description |
|---|---|---|
| `tox_ref` | `string` | Relative file path from the externalization folder to the child's `.tox` file. |

**Mutually exclusive with `children`**: When `tox_ref` is present, the operator definition does not contain a `children` array. The COMP's internal network is defined entirely in the referenced `.tox` file. This prevents the parent `.tdxn` from duplicating the contents of the child `.tox`, which would otherwise pollute `type_defaults` with the child's internal operator types and bloat the parent file.

**Resolution**: On import, the importer creates the COMP shell (name, type, position, parameters, flags) but does not populate its children. `externaltox` is **not** present in the parent `.tdxn`'s parameter dict (it's an Embody-managed parameter, excluded from TDXN export). Instead, the importer stores the `tox_ref` path on the new shell as `_pending_tox_restore` storage, then a post-import phase (`_restoreTOXShells`) sets `externaltox` from that marker and calls `_reloadTox` to load the `.tox` content immediately. This means the `.tox` content is fully restored after import — both for runtime imports (e.g. `import_network` via MCP) and for project-open reconstruction. `ext.Embody.restoreTOXComps` (frame 45) still handles the case where the parent `.tdxn` is not re-imported and the table is the only source.

**TOX vs TDXN, when to use which**:

- **TOX**: opaque binary encapsulation. The `.tox` is a single self-contained file, fast to load, but not git-diffable. Suitable for palette widgets, third-party COMPs, and anything where you don't need text-level review of internals.
- **TDXN**: text/YAML snapshot of the network. Fully git-diffable. Use this when you want pull requests to show changes to the COMP's internals.

Both strategies receive the same ownership treatment in parent `.tdxn` files — neither embeds children into the parent. The strategy choice is about the **child file format**, not whether the parent embeds.

**Backward compatibility**:

- Pre-v1.4 `.tdn` files that embedded TOX children's contents still import correctly: on import, `_stripNestedTOXChildren` consults the externalizations table and clears any embedded `children` for TOX-tagged paths. The COMP shell is created and `ext.Embody.restoreTOXComps` loads from the `.tox` file.
- Files **with** `tox_ref` imported by an older Embody that doesn't recognize the field will silently ignore it. The strip path handles the nested COMP correctly as a fallback.
- The `embed_all=True` export option suppresses `tox_ref` and inlines all children.

### Palette Clones

COMPs that originate from the TouchDesigner palette (e.g. `abletonLink`, Widget components, anything under `Samples/Palette/`) are detected and marked with `"palette_clone": true`. Their children are **not** exported because TouchDesigner automatically recreates them from the palette source when the project loads.

**Parameter handling for palette clones**: During export, parameters are compared against two baselines — the built-in default (`p.default`) and the clone source's actual value. If a parameter matches `p.default` but differs from the clone source, it is still exported. This prevents user-set values from being silently dropped when they happen to match the built-in default but not the clone source (e.g., a `buttontype` whose `p.default` is `"momentary"` but whose clone source is `"toggledown"`). The `clone` and `enablecloning` parameters are always excluded — TD auto-sets these during rebuild.

#### Palette Detection

Detection uses two strategies:

1. **Palette catalog** (primary): Embody ships a catalog at `embody/Embody/palette_catalog.tsv` built by scanning every `.tox` in TD's installed palette directory. The catalog records each component's `name`, `OPType`, and `min_children` count (264 entries for TD 099.2025.32280). A COMP is detected as a palette if its name matches a catalog entry, its `OPType` matches, and it has at least `min_children // 2` children (a floor that tolerates user modifications while rejecting empty user COMPs that happen to share a palette name).
2. **Clone expression heuristic** (fallback): if the `clone` parameter points to `/sys/` or references `TDBasicWidgets`, `TDResources`, or `TDTox`, the COMP is detected as a palette. Catches cases where the catalog doesn't cover the current TD build. **Exception**: paths and expressions under `/sys/TDTox/defaultCOMPs/` are explicitly excluded. That directory holds TD's native-operator templates — every freshly-created `buttonCOMP`, `panelCOMP`, etc. clones from there by default, and those are stock types, not palette components. Export them as regular COMPs.

The catalog is loaded into memory by `CatalogManagerExt.EnsureCatalogs()` at startup from the shipped TSV (skipping a runtime scan) or from `.embody/catalog_<build>.json` if already cached locally.

#### Palette Handling

When the export path encounters a detected palette COMP, the `Tdnpalettehandling` parameter on Embody's TDXN page decides what to do:

| Value | Behavior |
|---|---|
| `Ask` (default) | On first encounter of each palette COMP, prompts with four buttons: **Black Box** (this COMP), **Full Export** (this COMP), **Black Box for All** (flips the project-wide par), **Full Export for All** (flips the project-wide par). The per-COMP decision is persisted via `comp.store('_tdn_palette_handling', 'blackbox'|'fullexport')` so subsequent exports don't re-prompt. |
| `Black Box` | Always emit `"palette_clone": true` with parameter overrides only. Children are re-dropped from the palette on import. Correct for stock palette COMPs; lets upstream palette updates from Derivative flow through on round-trip. |
| `Full Export` | Always export all internal children as if the COMP were a regular user COMP. Use when you've heavily customized the palette internals and need that state preserved across round-trip. |

Per-COMP stored decisions take precedence over the project-wide par. To reset a COMP's stored decision, call `comp.unstore('_tdn_palette_handling')`.

---

## Annotations

Annotations are visual documentation elements in TouchDesigner networks (comments, network boxes, and annotate panels). They are stored in an `annotations` array at the top level (for root-level annotations) and optionally on each COMP operator (for nested annotations).

```yaml
operators: [ ... ]
annotations:
- name: annot_core
  mode: annotate
  title: Core Tests
  text: Unit tests for core functionality
  position: [-70, -300]
  size: [1070, 660]
  color: [0.5, 0.5, 0.5]
  opacity: 0.8
```

For nested COMPs, `annotations` appears alongside `children`:

```yaml
- name: container1
  type: baseCOMP
  children: [ ... ]
  annotations:
  - name: annot1
    mode: comment
    text: Signal processing chain
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
| `backAlpha` | number | No | Only if different from `1.0`. Background alpha; `0.0` is a value and round-trips. |
| `titleHeight` | number | No | Only if different from `30`. Title bar height. |
| `bodyFontSize` | number | No | Only if different from `10`. Body text size. |

### Export Behavior

Annotation COMPs (`annotateCOMP`) are serialized **exclusively** to the `annotations` array — never as `operators` entries. The exporter skips any `annotateCOMP` child of a network, even a non-utility one. This matters because TD's stock annotate is a *palette clone* with an extension and ~40 custom parameters: capturing it as a regular operator would dump well over 100 lines of `custom_pars`/`par_templates` per annotation that exactly duplicate (in a far heavier form) what the compact `annotations` entry already records. The `annotations` array is the single source of truth for annotations; the round-trip rebuilds them from it (see Import Behavior).

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

Importing a `.tdxn` file reconstructs the network in a pre-phase plus a series of ordered phases. This ordering ensures that dependencies are satisfied — for example, operators must exist before they can be connected, and positions are set last because creating operators may shift existing nodes.

When `clear_first` is set, existing children are destroyed before import — **except** COMPs carrying the exclude tag (the `Tdnexcludetag` parameter's value, `tdn_exclude` by default), which are preserved. Excluded COMPs are invisible to TDXN (absent from the `.tdxn`), so destroying them would be permanent data loss; the owning app manages their lifecycle instead.

| Phase | Action | Details |
|-------|--------|---------|
| Pre | **Resolve templates and defaults** | If `par_templates` is present, `$t` references in `custom_pars` are expanded to full definitions with value overrides. If `type_defaults` is present, shared properties are merged into each operator (`parameters` via dict merge, `flags`/`size`/`color`/`tags` via whole-value injection; operator-specific values take precedence). Stale entries matching a preserved excluded COMP, and children of nested TDXN/TOX-externalized COMPs, are dropped so their own files stay authoritative. |
| 1 | **Create operators** | All operators are created depth-first. COMPs are created first so their children can be placed inside them. |
| 2 | **Create custom parameters** | Custom parameter definitions are created on COMPs (pages, types, ranges, menu entries, defaults). |
| 2.5 | **Expand sequences** | Built-in/custom parameter sequences (`sequences` key) have their block counts and sequence parameters created before any values are set. *Added in v1.3.* |
| 3 | **Set parameter values** | Both built-in and custom parameter values are applied. `=` prefix sets expression mode, `~` prefix sets bind mode, all other values set constant mode. |
| 4 | **Set flags** | Operator flags are applied. Array entries without `-` prefix set the flag to `true`; entries with `-` prefix set to `false`. |
| 4a | **Warn about locked non-DATs** | Locked TOP/CHOP/SOP operators are flagged — the lock is preserved but the frozen pixel/channel/geometry data is not (see [Lock Flag Limitation](#lock-flag-limitation)). |
| 5 | **Wire connections** | Operator and COMP connections are established. Source references are resolved (sibling name first, then full path). Array position equals input index. |
| 6 | **Set DAT content** | Text or table data is loaded into DAT operators. |
| 6a | **Restore storage** | Storage key-value pairs are restored via `op.store()`. `$type` wrappers are deserialized to Python types (tuple, set, bytes). |
| 7 | **Set positions** | Node positions, sizes, colors, and comments are applied (later phases still follow). Missing position defaults to `[0, 0]`. |
| 7b | **Set docking** | Docking relationships between operators are restored. |
| 7a | **Create annotations** | Annotations are created from the `annotations` array (top-level and per-COMP). Each annotation is created as an `annotateCOMP` with `utility=True`, then its mode, title, body text, position, size, color, and opacity are set. |
| 8 | **Restore file links** | File/syncfile parameters are restored on externalized DATs. |
| 8.5 | **Restore TOX content** | `.tox` content is loaded into `tox_ref` shells so their internals are present immediately after import. |
| 8.6 | **Restore nested TDXN content** | `tdn_ref` shells are imported from their own `.tdxn` files (recursively, with an ancestor-chain cycle guard) so their internals are present immediately after import. Skipped by startup reconstruction and the post-save restore, whose own depth-sorted loops import every tracked TDXN COMP exactly once. See [COMP References](#comp-references-tdn_ref). |
| 9 | **Apply target COMP properties** | The target COMP's own type, parameters, flags, color, tags, and comment are applied — last, so extension reinit triggered by recreating source DATs cannot overwrite them. |

The importer accepts either a full `.tdxn` document (with metadata) or just the `operators` array directly.

### Extension Initialization Timing

!!! danger "Extensions initialize BEFORE TDXN import"
    When a TDXN COMP is reconstructed (on project open or after save), the COMP shell is created first and any extensions on it initialize immediately. The TDXN import runs **after** extension initialization, calling `ImportNetwork` with `clear_first=True` — which deletes all children and recreates them from the `.tdxn` file. This means any state set up by `onInitTD` inside the COMP is **overwritten**.

**Timeline on project open:**

| Step | Frame | What happens |
|------|-------|--------------|
| 1 | Early | COMP shell created (exists but empty) |
| 2 | Early | Extension `__init__` runs |
| 3 | End of frame | `onInitTD` fires — network may not exist yet |
| 4 | Frame 60 | `ext.Embody.reconstructTDNComps` runs `ImportNetwork(clear_first=True)` |
| 5 | Frame 60+ | All children deleted and recreated from `.tdxn` |

**Timeline on save (strip/restore cycle):**

| Step | What happens |
|------|--------------|
| 1 | Pre-save: children stripped from TDXN COMPs |
| 2 | `.toe` saved without TDXN children |
| 3 | Post-save: `ImportNetwork` re-imports children from `.tdxn` |
| 4 | Extensions may reinitialize during restore |

**Impact:** If an extension's `onInitTD` creates operators, sets parameter values, writes to storage, or builds any state inside the COMP, that work is destroyed by the import. This affects extensions that live inside TDXN COMPs **and** extensions whose ownerComp is a TDXN-strategy COMP.

**Solution:** Defer initialization using `run()` with `delayFrames`:

```python
def onInitTD(self):
    run('args[0].postInit()', self, delayFrames=5)

def postInit(self):
    """Runs after TDXN import completes. Safe to set up state."""
    pass
```

The deferred method must be **idempotent** — it will run on every project open, after every save, and on manual reimport. Use a delay of at least 5 frames to ensure all import phases have completed.

For full guidance on writing extensions that coexist with TDXN, see the [Extensions](../td-development/extensions.md#initialization-and-tdxn-import-timing) documentation.

### Version Compatibility

When importing a full `.tdxn` document, the importer checks the metadata fields for compatibility:

- **`version`**: Compared against the current TDXN format version. Because older files remain back-compatible, a warning is logged **only when the file's version is newer** than the running build (the genuinely risky direction, where the file may use schema this build does not understand). Equal or older versions import silently.
- **`td_build`**: Compared against the running TouchDesigner version. An informational message is logged if they differ, since operator types and parameter defaults may vary between TD builds.
- **`build`**: Logged for informational purposes, identifying which save iteration is being imported.

These checks are non-blocking — the import always proceeds regardless of mismatches.

---

## Virtual File System (VFS) — Not Supported

**TDXN does not capture a COMP's Virtual File System, and this is not planned.**
Files embedded in a COMP's VFS — fonts, images, shaders, lookup tables, anything
addressed as `vfs://...` — are **not exported and are lost** when the COMP is
reconstructed from its `.tdxn`.

Verified 2026-09-04: a COMP holding one embedded file exports with no trace of
the file or its payload, and the rebuilt COMP's VFS is empty. Any parameter or
script referencing `vfs://` fails to resolve after reconstruction.

**Why it is excluded.** TDXN's entire purpose is a line-diffable, mergeable text
representation of a network. A VFS holds arbitrary binary payloads; carrying them
inline (base64 or otherwise) would destroy line-level diffability and inflate
files without bound — a single embedded font would dwarf the network it belongs
to, and every re-export would churn the blob.

**What to do instead**, for a COMP that genuinely needs embedded files:

- **Externalize it with the TOX strategy rather than TDXN.** A `.tox` is
  TouchDesigner's own container format and *does* preserve VFS contents
  (verified 2026-09-04: embedded file and payload survive a `.tox` save and
  re-instantiate intact). The trade is the usual one — a `.tox` is binary and
  does not diff.
- **Or keep the assets as real files on disk** beside the project and reference
  them by path. Path-valued parameters round-trip through TDXN normally, so the
  network stays diffable and the assets stay in version control as themselves.

There is no warning-free middle ground: if a COMP carries VFS files and is
externalized as TDXN, those files do not come back.

---

## Round-Trip Guarantees

For most networks, export → import → re-export produces identical `.tdxn` output. The format is designed to be stable across round-trips, with a few documented exceptions.

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

**Palette clones** — On first export, a palette-cloned COMP is marked `"palette_clone": true` and its children are skipped. After import, TouchDesigner materializes the children from the clone source. A subsequent re-export will include those children as regular operators. This means the second export is larger than the first. Parameters that match `p.default` but differ from the clone source are preserved (see [Palette Clones](#palette-clones)).

**Color tolerance** — Colors within `0.01` per channel of the default gray `[0.545, 0.545, 0.545]` are treated as default and not exported. A color of `[0.55, 0.55, 0.55]` survives; `[0.546, 0.546, 0.546]` is dropped.

**Float precision** — Values are rounded to 10 decimal places on first export. This can change the last digits of very precise values (e.g., `3.14159265358979` → `3.1415926536`). After that first rounding, subsequent exports are stable.

**Type defaults recomputation** — Type defaults and parameter templates are recomputed from scratch on each export. If operator populations change between exports (operators added/removed), different properties may qualify as "unanimous" for type_defaults, and different pages may qualify as templates. The final network state is always identical, but the YAML structure may differ.

**Locked non-DAT data** — When a TOP, CHOP, or SOP is locked, TDXN preserves the lock flag but not the frozen pixel, channel, or geometry data. After import, the operator is locked but empty. See [Lock Flag Limitation](#lock-flag-limitation).

**Virtual File System** — A COMP's embedded VFS files are never exported and do not survive reconstruction. This is deliberate and permanent; see [Virtual File System (VFS) — Not Supported](#virtual-file-system-vfs-not-supported).

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

TDXN import is **best-effort** — individual failures should not abort the entire operation. This section describes the expected behavior for developers working with TDXN files.

### Unknown Fields

Developers should ignore unknown fields when parsing TDXN documents. This ensures forward compatibility — a file exported by a newer version of Embody can still be imported by an older version, with unrecognized fields silently skipped.

### Failure Modes

| Situation | Expected behavior |
|-----------|-------------------|
| Unknown field in any object | Ignore it. |
| Missing required field (`name`, `type`) on an operator | Skip that operator silently (no log emitted). |
| Missing connection source (operator not found) | Skip that connection, log a warning. |
| Unrecognized custom parameter `style` | Skip that parameter definition, log a warning. |
| Unrecognized flag name | Ignore it. |
| Invalid parameter value type | Attempt type coercion; if impossible, skip with a warning. |
| Version mismatch (`version`, `td_build`) | Proceed with import. For `version`, log a warning only when the file is newer than the running build; older or equal versions are silent. For `td_build`, log an informational message. |
| Target COMP type mismatch (`type` vs destination) | Log a warning, proceed with import. The file's `type` field is informational — import does not change the destination COMP's type. |
| Unknown `$t` template reference | Log a warning, skip that page. |
| Missing `type_defaults` entry for a type | No-op (operator uses its own properties). |
| Non-serializable storage value on export | Skip that value, log at DEBUG level. |
| Unknown `$type` in storage on import | Treat as plain dict, log a warning. |
| Failed `store()` call on import | Skip that key, log a warning. |

### General Principle

Log warnings for skipped items where practical (a few malformed cases, like an op-def missing `name`/`type`, are skipped silently) so the developer can inspect the result. Never abort an entire import because a single operator, parameter, or connection failed — the partial result is more useful than no result.

---

## Complete Example

A realistic `.tdxn` file demonstrating all major features:

```yaml
format: tdxn
version: '2.1'
build: 3
generator: Embody/6.0.4
td_build: '2025.32050'
source_file: MyProject.toe
exported_at: '2026-02-19T14:30:00Z'
network_path: /
type: baseCOMP
options:
  include_dat_content: true
  include_storage: true
type_defaults:
  baseCOMP:
    parameters:
      resizecomp: =me
      repocomp: =me
par_templates:
  about:
    - {name: Build, style: Int, label: Build Number, readOnly: true}
    - {name: Version, style: Str, label: Version, readOnly: true}
operators:
  - name: controller
    type: baseCOMP
    color: [0.2, 0.4, 0.8]
    comment: Main controller
    tags: [core]
    custom_pars:
      Controls:
        - name: Speed
          style: Float
          default: 1
          max: 10
          clampMin: true
          normMax: 5
          value: 2.5
        - name: Mode
          style: Menu
          menuNames: [linear, ease, bounce]
          menuLabels: [Linear, Ease In/Out, Bounce]
          value: 1
        - name: Color
          style: RGB
          clampMin: true
          clampMax: true
          values: [1, 0.5, 0]
      About:
        $t: about
        Build: 3
        Version: 1.0.0
    flags: [viewer]
    comp_inputs: [renderer]
    children:
      - name: noise1
        type: noiseTOP
        parameters:
          type: sparse
          amp: 0.8
          period: 2
          monochrome: true
          resolutionw: 1920
          resolutionh: 1080
      - name: level1
        type: levelTOP
        position: [300, 0]
        parameters:
          opacity: =parent().par.Speed / 10
        inputs: [noise1]
        flags: [display]
      - name: config
        type: tableDAT
        position: [0, -200]
        dat_content:
          - [key, value]
          - [resolution, 1920x1080]
          - [fps, '60']
        dat_content_format: table
        flags: [lock]
      - name: script1
        type: textDAT
        position: [300, -200]
        dat_content: |
          # Initialize
          print('Controller ready')
        dat_content_format: text
  - name: renderer
    type: baseCOMP
    position: [500, 0]
    size: [300, 150]
    custom_pars:
      About:
        $t: about
        Build: 1
        Version: 0.9.0
```

Key observations:

- **`type_defaults`**: Both `baseCOMP`s share `resizecomp` and `repocomp` expressions, so those are hoisted out of individual operators
- **`par_templates`**: The "About" page definition is shared between `controller` and `renderer`, with different values
- **Expression shorthand**: `=parent().par.Speed / 10` instead of a nested `{expr: ...}` mapping
- **Flags as arrays**: `[viewer]`, `[display]`, `[lock]`
- **Simplified connections**: `[noise1]` instead of a list of `{index: 0, source: noise1}` mappings
- **Optional position**: `noise1` at `[0, 0]` omits `position`; `controller` at `[0, 0]` also omits it
- **Compact formatting**: Short numeric/string vectors like `[300, 0]`, `[0.2, 0.4, 0.8]`, `[core]` use inline YAML flow style

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-02-19 | Initial release with 8 format optimizations: expression shorthand (`=`/`~` prefixes), flags as arrays, page-grouped custom parameters, type defaults, parameter templates, optional position, simplified connections, compact formatting. |
| 1.0 | 2026-02-22 | Extended `type_defaults` to support `flags`, `size`, `color`, and `tags` in addition to `parameters`. Backward-compatible: old importers ignore unknown keys, new importers handle files without the new keys. |
| 1.0 | 2026-03-01 | Added annotation support (`annotations` array at top level and per-COMP). Added Phase 7a to import process. Removed `file`/`syncfile` from SKIP_PARAMS so DAT file references are preserved in TDXN exports. Pre-save now auto-exports current state before stripping TDXN COMPs. |
| 1.3 | 2026-04-07 | Added built-in parameter sequence support (`sequences` key on operator objects). Operators with resizable parameter blocks (mathmixPOP, glslPOP, attributePOP, constantCHOP, etc.) now round-trip correctly. Added Phase 2.5 to import process. Sequence parameters excluded from `type_defaults` compression and `_buildParCache`. |
| 1.4 | 2026-05-XX | Added `tox_ref` for COMPs with their own TOX externalization (relative path to the child's `.tox`, mutually exclusive with `children`). |
| 1.5 | 2026-06-10 | A text DAT's `dat_content` may now be an **array of line-strings** (multi-line) as well as a plain string (single-line), rejoined with `\n` on import. Keeps `.tdxn` files readable and git-diffable line-by-line. Import is fully back-compatible with the v1.x string form. The version-mismatch warning now fires only when the file is newer than the running build. |
| 2.0 | 2026-06-10 | The on-disk format is now **YAML** (a strict JSON superset). Multi-line `dat_content` reverts to a **plain string** rendered as a YAML literal block scalar (`|`), preserving exact trailing newlines via `\|`/`\|-`/`\|+` chomping. Importers parse json-first (BOM/whitespace-stripped), so legacy tab-indented JSON `.tdn` (v1.x/v1.5) still load losslessly with no migration gate. Auto-created default docked compute DATs are no longer serialized (TD recreates them on import). Files are roughly 17% smaller and read top-to-bottom without escaped newlines. MIME type is now `application/yaml`. |
