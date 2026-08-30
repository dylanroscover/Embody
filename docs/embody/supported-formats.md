# Supported Operators & Formats

## COMPs

All COMPs except `engine`, `time`, and `annotate` can be externalized as `.tox` files (or `.tdxn` files with the TDXN strategy).

## DATs

The following DAT types can be externalized:

| DAT Type |
|----------|
| Text DAT |
| Table DAT |
| Execute DAT |
| Parameter Execute DAT |
| Parameter Group Execute DAT |
| CHOP Execute DAT |
| DAT Execute DAT |
| OP Execute DAT |
| Panel Execute DAT |

## File Formats

| Family | Formats |
|--------|---------|
| COMPs | `.tox`, `.tdxn` |
| DATs | `.py`, `.json`, `.xml`, `.html`, `.glsl`, `.frag`, `.vert`, `.txt`, `.md`, `.rtf`, `.csv`, `.tsv`, `.dat` |

`.tdxn` is a YAML document as of v2.0 (a strict JSON superset; legacy JSON `.tdn` files still import). See the [TDXN Specification](../tdxn/specification.md).

Networks externalized before Embody 6.1 carry the `.tdn` extension. Both are read, written, and round-tripped indefinitely -- Embody keeps writing whichever extension a COMP already uses, and only a *new* externalization mints `.tdxn`, so a project can hold a mix. In `externalizations.tsv` the `strategy` column reads `tdn` for both; that token is an internal identifier and is deliberately unchanged by the rename.

## Excluded Operators

The following cannot be externalized:

- **Clones and replicants** (and their children) — these are managed by TouchDesigner's clone system
- **Engine COMPs** — special execution containers
- **Time COMPs** — internal timeline management
- **Annotate COMPs** — visual annotations (not data-bearing operators)
