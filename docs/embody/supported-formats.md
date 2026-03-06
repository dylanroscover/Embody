# Supported Operators & Formats

## COMPs

All COMPs except `engine`, `time`, and `annotate` can be externalized as `.tox` files (or `.tdn` files with the TDN strategy).

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
| COMPs | `.tox`, `.tdn` |
| DATs | `.py`, `.json`, `.xml`, `.html`, `.glsl`, `.frag`, `.vert`, `.txt`, `.md`, `.rtf`, `.csv`, `.tsv`, `.dat` |

## Excluded Operators

The following cannot be externalized:

- **Clones and replicants** (and their children) — these are managed by TouchDesigner's clone system
- **Engine COMPs** — special execution containers
- **Time COMPs** — internal timeline management
- **Annotate COMPs** — visual annotations (not data-bearing operators)
