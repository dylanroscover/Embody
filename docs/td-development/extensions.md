# Extensions

TouchDesigner's extension system attaches Python classes to COMPs, providing organized, reusable behavior.

## Basics

An extension is a Python class defined in a text DAT, attached to a COMP via the `Extension` parameter. Methods can be "promoted" to be callable directly on the COMP.

```python
class MyExtension:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    def DoSomething(self):
        """Promoted method (uppercase) — callable as op.myComp.DoSomething()"""
        pass

    def helperMethod(self):
        """Non-promoted method — callable as op.myComp.ext.MyExtension.helperMethod()"""
        pass
```

## Lifecycle Methods

### `onDestroyTD(self)`

Called on the **old** extension instance before TD reinitializes with a new one. Essential for clean teardown.

```python
def onDestroyTD(self):
    """Clean up before reinitialization."""
    # Cancel timers, close connections, remove callbacks
    pass
```

!!! warning
    Without `onDestroyTD`, old extension instances linger in memory due to Python garbage collection issues (circular references, cached callbacks). Always implement it.

### `onInitTD(self)`

Called at the **end of the frame** after the extension initialized. Use for post-init setup that needs a fully-cooked network.

```python
def onInitTD(self):
    """Called after the frame the extension was created."""
    # Safe to access other extensions and cooked operators here
    pass
```

## Initialization and TDN Import Timing

!!! danger "Critical: `onInitTD` runs BEFORE TDN import"
    If your extension lives inside a TDN-strategy COMP (or the extension's ownerComp is one), `onInitTD` fires **before** TDN reconstruction completes. Any state your extension sets up — created operators, parameter values, stored data, internal network structure — is **overwritten** when the TDN import runs.

### Why this happens

Embody uses TDN (TouchDesigner Network) files to externalize COMP contents as diffable JSON. On project open and after every save, Embody reconstructs TDN COMPs by calling `ImportNetwork` with `clear_first=True` — this deletes all children inside the COMP and recreates them from the `.tdn` file.

The timing sequence on project open:

1. **COMP shell is created** — the COMP exists but its children haven't been imported yet
2. **Extension initializes** — `__init__` runs, then `onInitTD` fires at end of frame
3. **TDN import runs** (frame 60) — deletes all children, recreates network from `.tdn` file
4. **Extension state is lost** — anything `onInitTD` set up inside the COMP is gone

A similar sequence occurs on every **Ctrl+S** due to the strip/restore cycle: children are stripped before save, then re-imported afterward. Extensions may reinitialize during this process.

### The fix: defer initialization

Use `run()` with `delayFrames` to push your setup code past the TDN import:

```python
class MyFeatureExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    def onInitTD(self):
        # DON'T set up state here — it will be overwritten by TDN import.
        # Instead, defer to after the import completes:
        run('args[0].postInit()', self, delayFrames=5)

    def postInit(self):
        """Runs after TDN import is complete. Safe to set up state here."""
        # Create operators, set parameters, build internal state
        child = self.ownerComp.op('my_child')
        if child:
            child.par.value0 = self.computeInitialValue()
```

### Guidelines

| Rule | Reason |
|------|--------|
| **Always defer initialization inside TDN COMPs** | `onInitTD` fires before import — any setup is overwritten |
| **Make deferred init idempotent** | It may run multiple times: project open, every save, manual reimport |
| **Null-check operators in deferred init** | During strip phase, children are temporarily gone |
| **Use `store()` on the COMP for persistent state** | Storage on the COMP itself survives TDN import (it's preserved in phase 6a) |
| **Use a delay of at least 5 frames** | The import runs across multiple phases; 5 frames provides sufficient margin |

!!! tip "How to tell if you're inside a TDN COMP"
    Check whether your COMP (or an ancestor) has a TDN entry in the externalizations table. In Claude Code, call `get_externalizations` and look for a `tdn` strategy on the COMP path. If your extension is a child of a TDN-strategy COMP, this timing issue applies to you.

!!! note "Extensions outside TDN COMPs are unaffected"
    If your extension's ownerComp is **not** managed by TDN (e.g., it's a TOX-strategy COMP or not externalized at all), `onInitTD` behaves normally — no deferral needed.

## Extension Referencing

```python
# Promoted methods (uppercase) — called directly on the component:
op.Embody.Update()
op.Embody.Save()

# Non-promoted methods (lowercase) — through ext:
op.Embody.ext.Embody.getExternalizedOps()

# Check if extension exists:
if hasattr(op.myComp.ext, 'MyExtension'):
    op.myComp.ext.MyExtension.doSomething()
```

!!! danger "Never cache extension references"
    Extension instances become stale when TD reinitializes them (e.g., when source code changes on disk). Always call inline:

    ```python
    # CORRECT — always call inline:
    self.ownerComp.ext.Embody.SomeMethod()

    # WRONG — cached reference goes stale:
    ext = self.ownerComp.ext.Embody
    ext.SomeMethod()  # May call the dead old instance
    ```

## `extensionsReady` Guard

Parameter expressions that reference extension-promoted attributes must guard against initialization timing:

```python
# In a parameter expression:
parent().MyExtensionProperty if parent().extensionsReady else 0
```

Without this, TD raises "Cannot use an extension during its initialization."

## Creating Extensions via MCP

Use the `create_extension` Envoy tool to create a fully wired extension:

```
create_extension(
    parent_path="/project1",
    class_name="MyExtension",
    code="class MyExtension:\n    def __init__(self, ownerComp):\n        self.ownerComp = ownerComp"
)
```

This creates a baseCOMP with a text DAT containing the extension class, properly wired up and initialized.

## Naming Convention

Extension classes and their source DATs must follow the `NameExt` convention:

- `EmbodyExt` — class name `EmbodyExt`, DAT name `EmbodyExt`
- `EnvoyExt` — class name `EnvoyExt`, DAT name `EnvoyExt`
- `TestRunnerExt` — class name `TestRunnerExt`, DAT name `TestRunnerExt`
