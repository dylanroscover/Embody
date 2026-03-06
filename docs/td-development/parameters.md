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

Toggle parameters use `0`/`1`, not `"True"`/`"False"`:

```python
# Via MCP set_parameter:
set_parameter(op_path="/project1/base1", par_name="active", value="1")

# In Python:
op('base1').par.active = True   # Works
op('base1').par.active = 1      # Also works
```

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
