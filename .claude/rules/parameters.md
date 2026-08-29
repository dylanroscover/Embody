# Parameter Rules

## Custom Parameter Design

Creating or designing custom parameters -> MUST load /parameter-design FIRST.

**Code owns the schema; the user owns the value.** Every parameter-creation path must be get-or-create -- extensions reinitialize on every source save, so a blind `append*()` either raises or (worse, since `replace=True` is the default) destroys a same-named par the user had set. Never `Par.destroy()` on a style mismatch: that takes the value, expressions and exports with it. Recipe in `/parameter-design` (Ownership and Lifecycle).

**Route parameter callbacks through one dispatcher, not one promoted method per parameter.** An `elif par.name == ...` chain in a parexec DAT adds a public-tier method to the COMP for every branch. Name handlers `_on<Par>ValueChange(par, prev)` / `_on<Par>Pulse(par)` on the extension and `getattr` them from the DAT -- see `/parameter-design` (Parameter Callbacks) and the three tiers in `td-python.md`.

**A custom parameter is one of three state mechanisms**, and the only one that survives an extension reinit (`storage` is cleared; a `tdu.Dependency` is rebuilt). Picking between them: `/parameter-design` (Parameter, Storage, or Dependency?).

## Reading and Writing Values

- **Always use `.eval()`** to get a parameter's current runtime value. `.val` only returns the constant-mode value.
- **Setting `.val` silently switches mode to CONSTANT** -- destroys any active expression. Use assignment (`par.tx = 5`) only when you intend constant mode.
- **Toggle parameters** use `0`/`1` (not `"True"`/`"False"`). With `set_parameter`, pass `value="0"` or `value="1"`.
- **Explicit type conversion**: TD parameters remain TD objects internally. Convert with `int()`, `float()`, `str()` before passing to standard Python functions.

## OP-Reference Parameter Values

Parameters that reference operators (Camera, Geometry, Lights, TOP, CHOP, SOP, DAT, MAT) follow the same rules as code -- **never use absolute paths**. Use the shortest relative reference that resolves from the parameter's owner:

| Target location | Value format | Example |
|---|---|---|
| Sibling (same network) | Name only | `cam` |
| Child of self | `./child` | `./render1` |
| Up one level | `../name` | `../shared/lut` |

For references needing shortcuts or complex resolution, use expression mode instead of constant mode:

| Need | Expression |
|---|---|
| Parent shortcut | `parent.Scene.op('cam')` |
| Global shortcut | `op.Assets.op('texture1')` |

With `set_parameter`: use `value="cam"` for siblings, or `expr="parent.Scene.op('cam')"` for shortcut-based references.

