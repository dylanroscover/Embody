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
