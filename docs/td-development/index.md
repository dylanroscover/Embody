# TouchDesigner Development

This section covers TouchDesigner Python development patterns, best practices, and common pitfalls. While not specific to Embody, these patterns are essential for working effectively with TouchDesigner — especially when using Envoy and AI assistants.

## Essential Concepts

- **[Extensions](extensions.md)** — TouchDesigner's extension system for attaching Python classes to COMPs
- **[Parameters](parameters.md)** — Accessing, setting, and creating parameters correctly
- **[Threading](threading.md)** — Running background tasks without blocking TD's UI
- **[Common Mistakes](common-mistakes.md)** — The most frequent pitfalls and how to avoid them

## Quick Reference

### Pull-Based Cook Model

TouchDesigner uses a **pull-based** cook system — operators only cook when something downstream demands their output. Changing a parameter makes the node "dirty" but does NOT trigger an immediate cook.

- **Always-cook operators**: Output nodes (Movie File Out TOP, Audio Device Out CHOP, etc.) and Render TOPs cook every frame
- **Performance implication**: Nodes with no viewer and no downstream output skip cooking — minimize visible viewers

### `op()` vs `opex()`

```python
# op() returns None if not found — silent failure:
node = op('/nonexistent/path')
node.par.tx = 5  # AttributeError: 'NoneType' has no attribute 'par'

# opex() raises an exception immediately:
node = opex('/nonexistent/path')  # Raises tdError with clear message

# ops() returns a LIST of matching operators (supports wildcards):
all_noises = ops('noise*')  # [noise1, noise2, noise3, ...]
```

Use `opex()` when the operator must exist. Use `op()` only when `None` is an acceptable result.

### `debug()` vs `print()`

```python
debug('value is', x)  # Output: "myScript line 42: value is 42" (with source info)
print('value is', x)  # Output: "value is 42" (no source info)
```

Always prefer `debug()` — it includes the source DAT name and line number.

### Pre-Installed Python Packages

Available without installation: `numpy`, `cv2` (OpenCV), `requests`, `yaml` (PyYAML), `cryptography`, `attrs`

Auto-imported (no `import` needed): `math`, `re`, `sys`, `collections`, `enum`, `inspect`, `traceback`, `warnings`

### TD Documentation

- [Wiki home](https://docs.derivative.ca/Main_Page)
- [OP Class](https://docs.derivative.ca/OP_Class) — base operator class
- [COMP Class](https://docs.derivative.ca/COMP_Class) — component class
- [Par Class](https://docs.derivative.ca/Par_Class) — parameter class
- [DAT Class](https://docs.derivative.ca/DAT_Class) — DAT class
- [Extensions](https://docs.derivative.ca/Extensions) — extension system
- [Cook](https://docs.derivative.ca/Cook) — cook cycle documentation
