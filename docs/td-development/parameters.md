# Parameters

## Reading Parameter Values

Always use `.eval()` to get a parameter's current runtime value:

```python
# CORRECT — .eval() works in all modes (constant, expression, export, bind):
value = op('geo1').par.tx.eval()

# WRONG — .val only returns the constant-mode value:
value = op('geo1').par.tx.val
```

## Setting Parameters

```python
# These are equivalent:
op('geo1').par.tx = 5
op('geo1').par.tx.val = 5  # Also implicitly sets mode to constant

# Menu parameters accept both string name and index:
op('geo1').par.xord = 'trs'   # by name
op('geo1').par.xord = 5       # by index
```

!!! warning
    Setting `.val` **implicitly switches mode to CONSTANT**. If the parameter was in expression mode, the expression is silently destroyed:

    ```python
    op('geo1').par.tx.val = 5  # Kills any active expression!
    ```

## Toggle Parameters

Prefer `0`/`1` for toggle parameters:

```python
# Via MCP set_parameter:
set_parameter(op_path="/project1/base1", par_name="active", value="1")

# In Python:
op('base1').par.active = True   # Works
op('base1').par.active = 1      # Also works
```

!!! note
    Envoy's `set_parameter` also coerces the strings `"True"` / `"False"` for toggle parameters (anything other than `"0"`, `"false"`, `"False"`, or `""` becomes on). `"True"`/`"False"` are therefore not *invalid* — but `"0"`/`"1"` remain the clearer, preferred convention.

## Type Casting

Direct method calls on parameter values require explicit `.eval()`:

```python
# CORRECT:
me.par.tx.eval().hex()

# WRONG — parameter objects don't have .hex():
me.par.tx.hex()
```

When passing values to standard Python functions, explicitly convert:

```python
int(op('geo1').par.tx)
float(op('geo1').par.tx)
str(op('geo1').par.tx)
```

## Creating Custom Parameters

Custom parameters are created via `appendCustomPage()` on COMPs:

```python
page = comp.appendCustomPage('Controls')
pg = page.appendFloat('Speed', label='Speed')  # Returns ParGroup, NOT Par!
p = pg[0]                                       # Get the actual Par
p.default = 0.5
p.normMin = 0; p.normMax = 2    # Slider range
p.min = 0; p.clampMin = True    # Hard clamp
```

!!! important
    All `append*` methods return a **ParGroup** (tuple-like), not a single Par. Always index with `[0]` for single-value parameters.

### Available Types

```python
page.appendFloat('Speed')      # Float parameter
page.appendInt('Count')        # Integer
page.appendToggle('Active')    # Boolean toggle
page.appendStr('Label')        # String
page.appendMenu('Mode')        # Dropdown menu (empty — set menuNames/menuLabels)
page.appendPulse('Reset')      # Fire-once button
page.appendRGB('Color')        # Creates Colorr, Colorg, Colorb
page.appendXYZ('Pos')          # Creates Posx, Posy, Posz
page.appendOP('Target')        # Operator reference
page.appendFile('Path')        # File path selector
```

### Naming Rule

First letter MUST be uppercase, rest lowercase/numbers. No underscores. TD enforces this.

### Cleanup

```python
comp.destroyCustomPars()   # Remove ALL custom pars
par.Speed.destroy()        # Remove a single custom par
```

## `mod()` for Module Access

The `mod` object accesses DAT modules without `import` — essential in parameter expressions:

```python
# In a parameter expression (import not available):
mod.utils.myFunction()

# In a script (cache the reference for loops):
m = mod.utils
m.func()

# Access by path:
mod('/project1/utils').myFunction()
```

## Parameter Design: ownership, lifetime, and publishing

The deeper design doctrine — the same text Embody ships to AI agents as the `/parameter-design` skill — condensed for humans:

**Code owns the schema; the user owns the value.** Extensions reinitialize on every source save, so every parameter-creation path must be *get-or-create*, never create-blindly: probe the whole COMP by `tupletName` (multi-value styles store component pars, so `.name` probes miss them), and never `Par.destroy()` on a style mismatch — that takes the user's value, expressions, and exports with it. Embody ships a reference helper as `op.Embody.op('embody_pardef').module.ensureCustomPar`.

**Parameter, storage, or Dependency?** Pick by lifetime and audience, not taste:

| The state is | Use | Survives ext reinit | Survives tox reload / TDXN rebuild |
|---|---|---|---|
| User-facing, part of the COMP's interface | **custom parameter** | yes | yes |
| Durable bookkeeping the user shouldn't see | **`storage`** | **yes** | **no** (wiped) |
| A derived value that must recook its readers | **`tdu.Dependency`** | **no** (rebuild in `__init__`) | no |

(Probed on TD 2025.33070. The widespread belief that a reinit clears storage is wrong — storage lives on the COMP; what clears it is the COMP's *contents being replaced*.)

**Publish the value; do not push it.** Code that recomputes something and then assigns it onto each consumer (`other.par.Value = x` in a loop) goes stale the moment someone adds a reader. Publish once as a dependable — a `tdu.Dependency`, `store()`, or a custom parameter — and let readers pull; expressions recook on change automatically. Writing `comp.storage[k] = v` directly, mutating a fetched list in place, or setting a plain `self.attr` all change the value and notify **nobody** — always write through `store()` or `dep.val`.

### `TDF.createProperty` vs raw `tdu.Dependency`

```python
import TDFunctions as TDF

class MyExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        TDF.createProperty(self, 'Scale', value=1.0, dependable=True,
                           readOnly=False)   # expression: op('comp').Scale
        self.Raw = tdu.Dependency(5)          # expression: op('comp').Raw.val
```

- The two idioms need **different reader expressions** — `op('comp').Scale` for a created property, `op('comp').Raw.val` for a raw Dependency. Swapping them fails silently: the raw form without `.val` evaluates the Dependency *object*, which is always truthy and never changes.
- **The property's name is its access tier.** `createProperty(self, 'MyProperty', ...)` makes a capitalized instance attribute, and TD promotes those onto the COMP exactly like methods — an `Upper`-named dependable property *is* public API. Capitalize only what users and agents should read off the COMP; internal state stays `_lower`.
- `self.Raw = 5` **destroys** a raw Dependency (rebinds to a plain int, no error); write `self.Raw.val = 5`.
- **Binding is two-way.** A Dependency is a legal bind *master*: a parameter whose `bindExpr` resolves to it tracks `dep.val` — and editing the bound parameter writes back into the Dependency (verified on 2025.33070). Use bind mode when the parameter should be a control surface for the state; use an expression when it should only display it.
- Appending bound methods to `dep.callbacks` leaks a subscriber on every source-file save — remove them in `onDestroyTD`.
- Both are main-thread-only.

## Operator Storage

Persistent data storage on any operator:

```python
op('base1').store('count', 42)
val = op('base1').fetch('count', 0)  # 0 is default if missing
op('base1').unstore('count')
```

!!! warning
    `fetch()` searches **up the parent hierarchy** by default. Use `search=False` for local-only:

    ```python
    op('base1').fetch('key', 0, search=False)
    ```

## `tdu.Dependency` for Reactive Values

Wrap values so parameter expressions automatically recook when they change:

```python
dep = tdu.Dependency(0)
dep.val = 5          # CORRECT — triggers recooks
dep = 5              # WRONG — destroys the Dependency object!

# For mutable contents:
dep.val = [1, 2, 3]
dep.val.append(4)    # Does NOT trigger update
dep.modified()       # Required — notifies dependents
```

## `tdu` Utility Functions

```python
tdu.clamp(val, min, max)
tdu.remap(val, fromMin, fromMax, toMin, toMax)
tdu.rand(seed)                     # Deterministic random [0.0, 1.0)
tdu.base('noise3')                 # 'noise'
tdu.digits('noise3')               # 3
tdu.validName('my op!')            # 'my_op_'
tdu.match('noise*', ['noise1'])    # ['noise1']
```
