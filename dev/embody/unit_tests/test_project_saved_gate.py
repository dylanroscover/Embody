"""Off-TD contract tests for "is this project saved?".

ONE authority answers that question -- EmbodyExt._projectSavedOnDisk -- and
the answer comes from the project's NAME, not from a file at
project.folder / project.name. TouchDesigner reports project.name as the NEXT
name in an incremental series once a project has been saved, so the literal
path is routinely absent on a project that has been saved for months:
D:/node-touchdesigner/Control.35.toe on disk, project.name 'Control.36.toe'.
That drift refused to enable Convoy on a live production project
(field-reported 2026-08-19) and, earlier, silently closed the release smoke's
write gate on the pristine template (2026-08-10).

No TouchDesigner import: the module is loaded from source and its `project`
global is planted per test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
EMBODY_EXT = REPO / "dev" / "embody" / "Embody" / "EmbodyExt.py"
CONVOY_EXT = REPO / "dev" / "embody" / "Embody" / "convoy" / "ConvoyExt.py"
WIZARD_LOGIC = REPO / "dev" / "embody" / "Embody" / "wizard" / "logic.py"


class _Project:
    def __init__(self, folder, name):
        self.folder = folder
        self.name = name


def _ext_with(folder, name):
    """EmbodyExt's save-gate helpers bound to a planted `project` global."""
    spec = importlib.util.spec_from_file_location(
        "project_saved_gate_embodyext", EMBODY_EXT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.project = _Project(str(folder), name)
    cls = module.EmbodyExt

    class _Ext:
        _projectNameIsPlaceholder = staticmethod(cls._projectNameIsPlaceholder)
        _resolveProjectToe = staticmethod(cls._resolveProjectToe)
        _wizardRecoveryPoint = cls._wizardRecoveryPoint
        _projectSavedOnDisk = cls._projectSavedOnDisk

    return _Ext()


# -- the NAME decides ---------------------------------------------------

def test_placeholder_names_are_the_only_unsaved_names():
    for name in ("NewProject.toe", "NewProject.1.toe", "NewProject.12.toe",
                 "newproject.toe", ""):
        ext = _ext_with("C:/nowhere", name)
        assert ext._projectNameIsPlaceholder() is True, name
    for name in ("Control.35.toe", "Control.toe", "e3.toe",
                 "NewProjectile.toe", "MyNewProject.1.toe"):
        ext = _ext_with("C:/nowhere", name)
        assert ext._projectNameIsPlaceholder() is False, name


def test_a_saved_project_passes_without_a_file_at_project_name(tmp_path):
    """THE regression. Nothing on disk answers to project.name because TD
    already advanced the increment -- the project is still saved."""
    ext = _ext_with(tmp_path, "Control.36.toe")
    assert ext._projectSavedOnDisk() is True


def test_a_never_saved_project_is_still_refused(tmp_path):
    ext = _ext_with(tmp_path, "NewProject.1.toe")
    assert ext._projectSavedOnDisk() is False


def test_a_project_genuinely_saved_as_the_placeholder_name_passes(tmp_path):
    (tmp_path / "NewProject.1.toe").write_bytes(b"toe")
    ext = _ext_with(tmp_path, "NewProject.1.toe")
    assert ext._projectSavedOnDisk() is True


def test_saved_verdict_is_latched(tmp_path):
    ext = _ext_with(tmp_path, "Control.36.toe")
    assert ext._projectSavedOnDisk() is True
    assert ext._saved_on_disk is True


# -- resolving the real file through increment drift --------------------

def test_the_literal_path_wins_when_it_exists(tmp_path):
    (tmp_path / "Control.35.toe").write_bytes(b"toe")
    ext = _ext_with(tmp_path, "Control.35.toe")
    assert ext._resolveProjectToe() == str(tmp_path / "Control.35.toe")


def test_drift_falls_back_to_the_newest_file_in_the_series(tmp_path):
    for name in ("Control.34.toe", "Control.35.toe", "Control.toe"):
        (tmp_path / name).write_bytes(b"toe")
    import os
    import time
    now = time.time()
    os.utime(tmp_path / "Control.34.toe", (now - 300, now - 300))
    os.utime(tmp_path / "Control.toe", (now - 200, now - 200))
    os.utime(tmp_path / "Control.35.toe", (now, now))
    ext = _ext_with(tmp_path, "Control.36.toe")
    assert ext._resolveProjectToe() == str(tmp_path / "Control.35.toe")


def test_an_unrelated_toe_is_never_mistaken_for_this_project(tmp_path):
    (tmp_path / "SomethingElse.2.toe").write_bytes(b"toe")
    ext = _ext_with(tmp_path, "Control.36.toe")
    assert ext._resolveProjectToe() is None


def test_the_recovery_point_stays_file_based(tmp_path):
    """A recovery point you cannot reopen is not a recovery point -- the
    name-based rule must NOT leak into this one."""
    ext = _ext_with(tmp_path, "Control.36.toe")
    assert ext._wizardRecoveryPoint() is None
    (tmp_path / "Control.35.toe").write_bytes(b"toe")
    ext = _ext_with(tmp_path, "Control.36.toe")
    assert ext._wizardRecoveryPoint() == str(tmp_path / "Control.35.toe")


# -- one authority: the other two gates delegate ------------------------

def test_convoy_saved_gate_delegates_to_embody():
    src = CONVOY_EXT.read_text(encoding="utf-8")
    body = src.split("def _savedToe", 1)[1].split("\n    def ", 1)[0]
    assert "_projectSavedOnDisk" in body
    # Never again the verdict: a bare isfile on project.folder/project.name.
    assert "return path if os.path.isfile(path) else None" not in body


def test_wizard_save_gate_delegates_to_embody():
    body = WIZARD_LOGIC.read_text(encoding="utf-8").split(
        "def _projectSaved", 1)[1].split("\ndef ", 1)[0]
    assert "_projectSavedOnDisk" in body
