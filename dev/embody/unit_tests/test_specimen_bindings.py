"""specimen_bindings: published Specimens must carry no dev-only file bindings.

The lab's DATs are externalized, so a live export carries
`file: specimen_lab/<slug>/*.glsl` and `syncfile: true`. Per the Text DAT docs,
Sync to File creates the file on the first DAT update and writes every edit to
disk, so a pasted Specimen would write into the user's project. The June-era
published blobs carry none of it; the 2026-09-07 re-exports did (field
2026-09-11).

Pure stdlib, and no module-level pytest or yaml import: TestRunnerExt imports
every test_*.py in this folder under TD's Python.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("specimen_bindings", _ROOT / "dev" / "specimen_bindings.py")
sb = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sb
_spec.loader.exec_module(sb)

DOC = {
    "type_defaults": {
        "textDAT": {"parameters": {"language": "glsl", "syncfile": True}, "size": [130, 90]},
        "tableDAT": {"parameters": {"syncfile": True}},
    },
    "operators": [
        {"name": "glsl_pixel", "type": "textDAT",
         "parameters": {"file": "specimen_lab/demo/glsl_pixel.glsl", "extension": "frag"},
         "dat_content": "void main() {}"},
        {"name": "only_binding", "type": "textDAT",
         "parameters": {"file": "specimen_lab/demo/x.glsl", "syncfile": True}},
        {"name": "geo", "type": "geoCOMP", "children": [
            {"name": "file1", "type": "fileinPOP",
             "parameters": {"file": "=app.samplesFolder + '/Geo/defcam.tog'"}},
            {"name": "nested", "type": "textDAT",
             "parameters": {"file": "specimen_lab/demo/nested.glsl"}},
        ]},
    ],
}


def test_strip_removes_lab_bindings_and_keeps_real_content():
    doc = copy.deepcopy(DOC)
    removed = sb.strip_bindings(doc)
    assert removed == 6, removed
    # The lab file binding goes; the DAT and its inline shader stay.
    pixel = doc["operators"][0]
    assert pixel["parameters"] == {"extension": "frag"}
    assert pixel["dat_content"] == "void main() {}"
    # A parameters block left empty is dropped entirely.
    assert "parameters" not in doc["operators"][1]
    # Nested children are walked.
    geo = doc["operators"][2]["children"]
    assert "parameters" not in geo[1]
    # An expression pointing at TD's own samples is NOT a dev binding.
    assert geo[0]["parameters"] == {"file": "=app.samplesFolder + '/Geo/defcam.tog'"}
    # type_defaults: the sync flag goes, real defaults stay, empty entries vanish.
    assert doc["type_defaults"]["textDAT"]["parameters"] == {"language": "glsl"}
    assert "tableDAT" not in doc["type_defaults"]


def test_strip_is_idempotent():
    doc = copy.deepcopy(DOC)
    sb.strip_bindings(doc)
    once = copy.deepcopy(doc)
    assert sb.strip_bindings(doc) == 0
    assert doc == once


def test_is_dev_file_only_matches_plain_lab_paths():
    assert sb.is_dev_file("specimen_lab/demo/x.glsl")
    assert sb.is_dev_file("specimen_lab\\demo\\x.glsl")
    assert not sb.is_dev_file("=app.samplesFolder + '/Geo/defcam.tog'")
    assert not sb.is_dev_file("assets/x.glsl")
    assert not sb.is_dev_file(None)


def test_text_pass_matches_the_dict_pass_and_keeps_formatting():
    import yaml  # not at module level: TD's Python may lack it

    text = yaml.safe_dump(copy.deepcopy(DOC), sort_keys=False)
    after, removed = sb.strip_text(text)
    assert removed == 6, removed
    expected = copy.deepcopy(DOC)
    sb.strip_bindings(expected)
    assert yaml.safe_load(after) == expected
    # Surviving lines are preserved verbatim, not re-serialised. (An entry left
    # empty by the strip -- tableDAT, whose only parameter was syncfile -- goes,
    # which is what the dict pass does too.)
    for line in ("      language: glsl", "    extension: frag",
                 "  dat_content: void main() {}",
                 "      file: =app.samplesFolder + '/Geo/defcam.tog'"):
        assert line in after, line
    assert "tableDAT" not in after


def test_committed_specimens_carry_no_dev_bindings():
    import yaml

    found = []

    def walk(ops, path):
        for op in ops or []:
            if not isinstance(op, dict):
                continue
            pars = op.get("parameters") or {}
            for par in sb.SYNC_PARS:
                if par in pars:
                    found.append(f"{path}:{op.get('name')}:{par}")
            if sb.is_dev_file(pars.get("file")):
                found.append(f"{path}:{op.get('name')}:file={pars['file']}")
            walk(op.get("children"), path)
            walk(op.get("operators"), path)

    files = sorted((_ROOT / "specimens").glob("*/*.tdxn"))
    assert files, "no committed specimens found"
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        walk(doc.get("operators"), path.name)
        for op_type, spec in (doc.get("type_defaults") or {}).items():
            pars = (spec or {}).get("parameters") or {}
            for par in sb.SYNC_PARS:
                if par in pars:
                    found.append(f"{path.name}:type_defaults.{op_type}:{par}")
            if sb.is_dev_file(pars.get("file")):
                found.append(f"{path.name}:type_defaults.{op_type}:file")
    assert not found, "published Specimens carry dev-only bindings: " + ", ".join(found)


def test_publisher_strips_before_writing():
    # The publish hook must run the transform on every export (field 2026-09-11).
    source = (_ROOT / "dev" / "specimen_publish.py").read_text(encoding="utf-8")
    assert "_strip_dev_bindings(new)" in source
    assert source.index("_strip_dev_bindings(new)") < source.index("_tdxn_content_equal")
