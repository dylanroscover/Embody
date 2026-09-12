"""Strip dev-only file bindings from published Specimens.

The specimen lab's DATs are externalized, so a live export carries
`file: specimen_lab/<slug>/*.glsl` plus `syncfile: true`. Per the Text DAT
docs, Sync to File creates the file on first update and writes every edit to
disk -- a pasted Specimen would write into the user's project (field
2026-09-11; the June-era published blobs carry none of it).

`strip_bindings` (dict, stdlib only) runs inside TD from specimen_publish.py;
`strip_text` (YAML text) cleans committed .tdxn files and is the CLI:

    python dev/specimen_bindings.py specimens/*/*.tdxn
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Tuple

# `file` is stripped ONLY when it points into the dev lab: a Specimen may hold a
# legitimate one (noise-terrain reads TD's own samplesFolder). The three sync
# parameters are DAT-only in TD, so they are stripped wherever they appear.
DEV_FILE_PREFIX = "specimen_lab/"
SYNC_PARS = ("syncfile", "loadonstart", "write")


def is_dev_file(value: Any) -> bool:
    """A plain (non-expression) path into the specimen lab."""
    return (
        isinstance(value, str)
        and not value.startswith("=")
        and value.replace("\\", "/").startswith(DEV_FILE_PREFIX)
    )


def _strip_params(params: Any) -> int:
    if not isinstance(params, dict):
        return 0
    removed = sum(1 for par in SYNC_PARS if params.pop(par, None) is not None)
    if is_dev_file(params.get("file")):
        del params["file"]
        removed += 1
    return removed


def _strip_ops(ops: Any) -> int:
    removed = 0
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        removed += _strip_params(op.get("parameters"))
        if isinstance(op.get("parameters"), dict) and not op["parameters"]:
            del op["parameters"]
        removed += _strip_ops(op.get("operators"))
        removed += _strip_ops(op.get("children"))
    return removed


def strip_bindings(doc: Dict[str, Any]) -> int:
    """Remove dev-only file bindings from a parsed TDXN document, in place.

    Returns how many parameters were removed. Idempotent.
    """
    removed = _strip_ops(doc.get("operators"))
    removed += _strip_params(doc.get("parameters"))
    defaults = doc.get("type_defaults")
    if isinstance(defaults, dict):
        for _op_type, spec in list(defaults.items()):
            if not isinstance(spec, dict):
                continue
            removed += _strip_params(spec.get("parameters"))
            if isinstance(spec.get("parameters"), dict) and not spec["parameters"]:
                del spec["parameters"]
            if not spec:
                del defaults[_op_type]
        if not defaults:
            del doc["type_defaults"]
    return removed


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def strip_text(text: str) -> Tuple[str, int]:
    """Same removal over TDXN YAML text, preserving the exporter's formatting.

    Drops each binding line, and a `parameters:` key left with no children.
    Returns (new_text, removed_count).
    """
    lines = text.splitlines(keepends=True)
    drop = [False] * len(lines)
    removed = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        name, _, value = stripped.partition(":")
        if name not in SYNC_PARS and not (name == "file" and is_dev_file(value.strip().strip("'\""))):
            continue
        # Only a mapping entry directly under a `parameters:` block counts.
        depth = _indent(line)
        for j in range(i - 1, -1, -1):
            if drop[j] or not lines[j].strip():
                continue
            up = _indent(lines[j])
            if up >= depth:
                continue
            if lines[j].strip().rstrip(":") == "parameters":
                drop[i] = True
                removed += 1
            break
    # A mapping key whose every child was dropped goes too (a `parameters:` left
    # empty, then the `textDAT:` type_default holding only it, and so on).
    changed = True
    while changed:
        changed = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if drop[i] or not stripped.endswith(":") or stripped.startswith("-"):
                continue
            depth, kept, saw_child = _indent(line), False, False
            for j in range(i + 1, len(lines)):
                if not lines[j].strip():
                    continue
                if _indent(lines[j]) <= depth:
                    break
                saw_child = True
                if not drop[j]:
                    kept = True
                    break
            if saw_child and not kept:
                drop[i] = True
                changed = True
    return "".join(l for i, l in enumerate(lines) if not drop[i]), removed


def _verify(before_text: str, after_text: str) -> None:
    """The text pass must equal the dict pass. Raises on any other change."""
    import yaml  # CLI-only; TD never takes this path

    expected = yaml.safe_load(before_text)
    strip_bindings(expected)
    actual = yaml.safe_load(after_text)
    if actual != expected:
        raise SystemExit("strip_text changed something other than the bindings")


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    total = 0
    for path in argv:
        with open(path, encoding="utf-8", newline="") as handle:
            before = handle.read()
        after, removed = strip_text(before)
        _verify(before, after)
        if removed or after != before:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(after)
        total += removed
        print(f"{path}: removed {removed}")
    print(f"total removed: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
