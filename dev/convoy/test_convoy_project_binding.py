"""Project-side automatic Convoy binding persistence, without TouchDesigner."""

import importlib.util
import json
from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "embody" / "Embody" /
          "embody_admin.py")
SPEC = importlib.util.spec_from_file_location(
    "convoy_test_embody_admin", SOURCE)
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


class FakeExt:
    def __init__(self, root):
        self.root = Path(root)
        self.logs = []

    def _findProjectRoot(self):
        return self.root

    def Log(self, message, level="INFO"):
        self.logs.append((level, str(message)))


def read_project(root):
    return json.loads((Path(root) / ".embody" / "project.json").read_text(
        encoding="utf-8"))


def write_project(root, value):
    path = Path(root) / ".embody" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def test_new_consent_records_an_uncommitted_candidate_and_preserves_keys(
        tmp_path):
    write_project(tmp_path, {"foreign": {"keep": True}})
    ext = FakeExt(tmp_path)
    candidate = "cv_" + "1" * 16
    assert admin.ensure_convoy_id(ext, candidate) == candidate
    project = read_project(tmp_path)
    assert project["foreign"] == {"keep": True}
    assert project["convoy"]["id"] == candidate
    assert project["convoy"]["binding_state"] == "candidate"


def test_legacy_persisted_id_is_safely_established(tmp_path):
    convoy_id = "cv_" + "2" * 16
    write_project(tmp_path, {"convoy": {
        "id": convoy_id,
        "consent_scope": admin.CONVOY_SCOPE_LAN,
        "granted_at": "2026-01-01T00:00:00Z",
    }})
    ext = FakeExt(tmp_path)
    assert admin.read_convoy_id(ext) == convoy_id
    assert admin.read_convoy_binding_state(ext) == "established"


def test_scope_upgrade_never_demotes_an_established_binding(tmp_path):
    convoy_id = "cv_" + "3" * 16
    write_project(tmp_path, {"convoy": {
        "id": convoy_id,
        "binding_state": "established",
        "consent_scope": admin.CONVOY_SCOPE_LOCAL,
        "granted_at": "2026-01-01T00:00:00Z",
    }})
    ext = FakeExt(tmp_path)
    assert admin.ensure_convoy_id(
        ext, convoy_id, admin.CONVOY_SCOPE_LAN,
        binding_state="candidate") == convoy_id
    assert read_project(tmp_path)["convoy"]["binding_state"] == "established"


def test_candidate_can_adopt_and_commit_host_authoritative_realm(tmp_path):
    old_id = "cv_" + "4" * 16
    new_id = "cv_" + "0" * 16
    write_project(tmp_path, {"foreign": 7, "convoy": {
        "id": old_id,
        "binding_state": "candidate",
        "consent_scope": admin.CONVOY_SCOPE_LAN,
        "granted_at": "2026-01-01T00:00:00Z",
    }})
    ext = FakeExt(tmp_path)
    assert admin.adopt_convoy_id(
        ext, new_id, old_id, "established") == new_id
    project = read_project(tmp_path)
    assert project["foreign"] == 7
    assert project["convoy"]["id"] == new_id
    assert project["convoy"]["binding_state"] == "established"
    assert project["convoy"]["consent_scope"] == admin.CONVOY_SCOPE_LAN


def test_stale_cas_cannot_replace_a_newer_project_binding(tmp_path):
    current_id = "cv_" + "5" * 16
    write_project(tmp_path, {"convoy": {
        "id": current_id,
        "binding_state": "candidate",
        "consent_scope": admin.CONVOY_SCOPE_LAN,
    }})
    ext = FakeExt(tmp_path)
    assert admin.adopt_convoy_id(
        ext, "cv_" + "6" * 16, "cv_" + "7" * 16) == ""
    assert read_project(tmp_path)["convoy"]["id"] == current_id


def test_established_binding_never_silently_switches_realms(tmp_path):
    old_id = "cv_" + "8" * 16
    write_project(tmp_path, {"convoy": {
        "id": old_id,
        "binding_state": "established",
        "consent_scope": admin.CONVOY_SCOPE_LAN,
    }})
    ext = FakeExt(tmp_path)
    assert admin.adopt_convoy_id(
        ext, "cv_" + "9" * 16, old_id, "established") == ""
    assert read_project(tmp_path)["convoy"]["id"] == old_id


def test_corrupt_project_is_never_overwritten(tmp_path):
    path = Path(tmp_path) / ".embody" / "project.json"
    path.parent.mkdir(parents=True)
    original = "{not-json\n"
    path.write_text(original, encoding="utf-8")
    ext = FakeExt(tmp_path)
    assert admin.ensure_convoy_id(ext, "cv_" + "a" * 16) == ""
    assert path.read_text(encoding="utf-8") == original

