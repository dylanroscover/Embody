# Common Mistakes

A comprehensive list of the most frequent TouchDesigner Python pitfalls and how to avoid them.

## Parameter Access

### 1. Using `.val` instead of `.eval()`

`.val` only returns the constant-mode value. If the parameter is in expression or bind mode, `.val` gives you the wrong answer.

```python
# WRONG:
value = op('geo1').par.tx.val

# CORRECT:
value = op('geo1').par.tx.eval()
```

### 2. Setting `.val` destroys expressions

Setting `.val` implicitly switches the parameter to constant mode, silently killing any active expression.

```python
# This destroys the expression:
op('geo1').par.tx.val = 5

# If you need to set a constant value, this is equivalent but more explicit:
op('geo1').par.tx = 5
```

### 3. Toggle parameters with wrong types

Toggle parameters use `0`/`1`, not `"True"`/`"False"` strings.

```python
# WRONG:
set_parameter(op_path="/base1", par_name="active", value="True")

# CORRECT:
set_parameter(op_path="/base1", par_name="active", value="1")
```

## Module-Level Code

### 4. `op()` at module scope

Never call `op()`, `parent()`, or access any TD objects at the top level of a `.py` file. Module-level code executes during import, potentially before the network is ready.

```python
# WRONG — executes during import:
my_op = op('base1')  # May be None

class MyExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

# CORRECT — defer to methods:
class MyExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    def doSomething(self):
        my_op = op('base1')  # Resolved at call time
```

### 5. Import shadowing

A DAT named `json` will shadow Python's stdlib `json` module. Name DATs carefully to avoid conflicts.

## Operator References

### 6. Using `op()` when `opex()` would catch errors

`op()` returns `None` silently. `opex()` raises an exception immediately with a clear error message.

```python
# Silent failure — hard to debug:
node = op('/nonexistent')
node.par.tx = 5  # AttributeError: NoneType

# Immediate failure — clear error:
node = opex('/nonexistent')  # Raises tdError
```

### 7. Relying on `op.id` for tracking

Operator IDs change across sessions, copy/paste, and undo. Never use them as persistent identifiers.

## Extension Pitfalls

### 8. Caching extension references

Extension instances become stale when TD reinitializes them (e.g., when externalized `.py` files change on disk).

```python
# WRONG — cached ref goes stale:
ext = self.ownerComp.ext.Embody
ext.SomeMethod()

# CORRECT — always call inline:
self.ownerComp.ext.Embody.SomeMethod()
```

### 9. Missing `extensionsReady` guard

Parameter expressions referencing promoted attributes need a guard:

```python
# In a parameter expression:
parent().MyProperty if parent().extensionsReady else 0
```

## Thread Safety

### 10. Accessing ThreadManager from a worker thread

`op.TDResources.ThreadManager` is a TD COMP — accessing it from a worker thread triggers a THREAD CONFLICT. Use plain `threading.Thread` for sub-tasks inside workers.

### 11. TD imports in worker threads

Never import or call TouchDesigner modules in worker thread code. All TD access must route through main-thread hooks.

## Data Access

### 12. `fetch()` without `search=False`

By default, `fetch()` searches up the parent hierarchy, which may return a parent's value.

```python
# May return a parent's value:
val = op('base1').fetch('key', 0)

# Local-only lookup:
val = op('base1').fetch('key', 0, search=False)
```

### 13. `mod.name` in loops

`mod.name` re-resolves the DAT lookup every call. Cache the reference:

```python
# SLOW — resolves every iteration:
for i in range(100):
    mod.utils.func(i)

# FAST — cached reference:
m = mod.utils
for i in range(100):
    m.func(i)
```

### 14. Assigning to `tdu.Dependency`

```python
dep = tdu.Dependency(0)
dep.val = 5    # CORRECT — updates the value
dep = 5        # WRONG — destroys the Dependency object!
```

## Performance

### 15. `TOP.sample()` in loops

Downloads the entire texture from GPU to CPU per call. Use `numpyArray()` for batch access.

```python
# WRONG — downloads entire texture per pixel:
for x in range(100):
    r, g, b, a = op('noise1').sample(x=x/100, y=0.5)

# CORRECT — single download:
arr = op('noise1').numpyArray()
```

### 16. `addError()`/`addWarning()` outside cook callbacks

These silently do nothing outside a cook callback. Use `addScriptError()` from extension methods.

## Operator Operations

### 17. `changeType()` without capturing return value

The original operator reference becomes invalid after `changeType()`:

```python
# WRONG:
old_op.changeType(waveCHOP)
old_op.par.tx = 5  # Invalid reference!

# CORRECT:
new_op = old_op.changeType(waveCHOP)
new_op.par.tx = 5
```

### 18. `COMP.copy()` for multiple connected operators

Connections between copied operators are lost. Use `copyOPs([list])` to preserve wiring:

```python
# Connections between a, b are lost:
new_a = comp.copy(a)
new_b = comp.copy(b)

# Connections preserved:
new_ops = comp.copyOPs([a, b])
```

## File Paths

### 19. Backslashes in paths

Always use forward slashes for cross-platform compatibility:

```python
# WRONG:
path = 'dev\\embody\\script.py'

# CORRECT:
path = 'dev/embody/script.py'
```

## Network Layout

### 20. Creating operators without setting positions

All new operators default to `[0, 0]` and stack on top of each other. Always set explicit positions after creating operators.

### 21. Relying on `layout()`

TD's `layout()` method produces unreadable layouts with no logical grouping. Calculate explicit positions for production networks.
