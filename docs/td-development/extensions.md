# Extensions

TouchDesigner's extension system attaches Python classes to COMPs, providing organized, reusable behavior.

## Basics

An extension is a Python class defined in a text DAT, attached to a COMP via the `Extension` parameter. TouchDesigner promotes **every capitalized member** of a promoted extension — methods *and* class constants — to the COMP itself. There is no per-member opt-out, so the capital letter is effectively the access modifier.

That gives you three tiers, not two. Choose by who calls the member, never by convenience:

| Tier | Name | Reached as | Who calls it |
|---|---|---|---|
| 1. Public API | `UpperCamelCase` | `op.myComp.DoSomething()` | A user or an agent, deliberately |
| 2. Wiring | `lowerCamelCase` | `op.myComp.ext.MyExtension.onFrame()` | The COMP's own exec / callback / parexec DATs |
| 3. Private | `_lowerCamelCase` | inside the class only | The class itself |

```python
class MyExtension:
    _CHUNK = 64                     # tier 3 -- a bare CHUNK would be promoted too

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp  # navigate from here, not from a bare parent()

    def DoSomething(self):
        """Tier 1 -- part of the component's public interface."""
        pass

    def onFrame(self):
        """Tier 2 -- called by this COMP's own Execute DAT, via
        op.myComp.ext.MyExtension.onFrame(). Promoting a frame hook is a
        design flaw, not a shortcut."""
        pass

    def _rebuildIndex(self):
        """Tier 3 -- called only from inside this class."""
        pass
```

**The test: could a user or an agent reasonably call this on the COMP?** If not, it belongs in tier 2 or 3.

The harm from an over-wide tier 1 is concrete. Extensions co-mounted on one COMP share a single namespace, and TouchDesigner documents no precedence for a duplicate promoted name, so one of the two becomes silently unreachable. Every promoted name is also a live `getattr` target for any code that dispatches by name. Note that promoted members do *not* appear in `dir()` -- they resolve through TD's `__getattr__` -- so this is an API-surface argument, not an autocomplete one.

!!! warning "`ext.<Name>` uses the Extension Name parameter, not the class name"
    If the COMP's `Extension Name` is `MyFeature` and the class is `MyFeatureExt`, then `op.myComp.ext.MyFeature.helperMethod()` works and `op.myComp.ext.MyFeatureExt.helperMethod()` raises. Embody's own extensions are named `Embody`, `Envoy`, `TDXN` and `CatalogManager` against classes suffixed `Ext`.

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

## Initialization and TDXN Import Timing

!!! danger "Critical: `onInitTD` runs BEFORE TDXN import"
    If your extension lives inside a TDXN-strategy COMP (or the extension's ownerComp is one), `onInitTD` fires **before** TDXN reconstruction completes. Any state your extension sets up — created operators, parameter values, stored data, internal network structure — is **overwritten** when the TDXN import runs.

### Why this happens

Embody uses TDXN (TouchDesigner eXternal Network) files to externalize COMP contents as diffable YAML. When Embody reconstructs a TDXN COMP it calls `ImportNetwork` with `clear_first=True` — this deletes all children inside the COMP and recreates them from the `.tdxn` file.

**When reconstruction runs depends on `Tdnmode`.** In the experimental **Roundtrip** mode, every TDXN COMP is reconstructed on project open (and stripped/restored on every save), so the timing below applies in full. In the default **Export-on-Save** mode the `.toe` stays authoritative on open — existing COMPs are **not** rebuilt, only a COMP *absent* from the `.toe` is reconstructed from its `.tdxn`, and save does not strip. Even so, treat the deferral pattern below as the safe default: it also covers manual `import_network` reloads, which use `clear_first=True` in any mode.

The timing sequence on project open (Roundtrip mode, or an absent-COMP recovery):

1. **COMP shell is created** — the COMP exists but its children haven't been imported yet
2. **Extension initializes** — `__init__` runs, then `onInitTD` fires at end of frame
3. **TDXN import runs** (frame 60) — deletes all children, recreates network from `.tdxn` file
4. **Extension state is lost** — anything `onInitTD` set up inside the COMP is gone

In **Roundtrip** mode a similar sequence occurs on every **Ctrl+S** due to the strip/restore cycle: children are stripped before save, then re-imported afterward. Extensions may reinitialize during this process. (Export-on-Save does not strip, so this save-time cycle does not apply there.)

### The fix: defer initialization

Use `run()` with `delayFrames` to push your setup code past the TDXN import:

```python
class MyFeatureExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    def onInitTD(self):
        # DON'T set up state here — it will be overwritten by TDXN import.
        # Instead, defer to after the import completes:
        run('args[0].postInit()', self, delayFrames=5)

    def postInit(self):
        """Runs after TDXN import is complete. Safe to set up state here."""
        # Create operators, set parameters, build internal state
        child = self.ownerComp.op('my_child')
        if child:
            child.par.value0 = self.computeInitialValue()
```

### Guidelines

| Rule | Reason |
|------|--------|
| **Always defer initialization inside TDXN COMPs** | `onInitTD` fires before import — any setup is overwritten |
| **Make deferred init idempotent** | It may run multiple times: project open, every save, manual reimport |
| **Null-check operators in deferred init** | During strip phase, children are temporarily gone |
| **Use `store()` on the COMP for persistent state** | Storage on the COMP itself survives TDXN import (it's preserved in phase 6a) |
| **Use a delay of at least 5 frames** | The import runs across multiple phases; 5 frames provides sufficient margin |

!!! tip "How to tell if you're inside a TDXN COMP"
    Check whether your COMP (or an ancestor) has a TDXN entry in the externalizations table. In Claude Code, call `get_externalizations` and look for a `tdn` strategy on the COMP path. If your extension is a child of a TDXN-strategy COMP, this timing issue applies to you.

!!! note "Extensions outside TDXN COMPs are unaffected"
    If your extension's ownerComp is **not** managed by TDXN (e.g., it's a TOX-strategy COMP or not externalized at all), `onInitTD` behaves normally — no deferral needed.

## Extension Referencing

```python
# Promoted methods (uppercase) — called directly on the component:
op.Embody.Update()
op.Embody.Save('/path/to/comp')   # Save() requires a COMP path argument

# Non-promoted methods (lowercase) — through ext:
op.Embody.ext.Embody.getExternalizedOps(COMP)

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
