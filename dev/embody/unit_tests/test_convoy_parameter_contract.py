"""Off-TD contract tests for Convoy's user-facing parameter scaffold.

These tests intentionally do not import TouchDesigner. They pin the source TDN
that creates the page, the nested copy used by the development network, and the
fail-closed projection helpers that can be exercised with plain Python fakes.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[3]
EMBODY_TDN = REPO / "dev" / "embody" / "Embody.tdn"
ROOT_TDN = REPO / "dev" / "embody.tdn"
CONVOY_EXT = REPO / "dev" / "embody" / "Embody" / "convoy" / "ConvoyExt.py"
EMBODY_EXT = REPO / "dev" / "embody" / "Embody" / "EmbodyExt.py"


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _convoy_page(document, nested=False):
    if nested:
        embody = next(row for row in document["operators"]
                      if row.get("name") == "Embody")
        return embody["custom_pars"]["Convoy"]
    return document["custom_pars"]["Convoy"]


def _by_name(rows):
    return {row["name"]: row for row in rows}


def _class_assignment(path: Path, class_name: str, assignment: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node.value for node in cls.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id == assignment for target in node.targets))


def _load_convoy_ext_class():
    spec = importlib.util.spec_from_file_location(
        "convoy_parameter_contract_ext", CONVOY_EXT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.ConvoyExt


class _Par:
    def __init__(self, value):
        self._value = value

    def eval(self):
        return self._value

    @property
    def val(self):
        return self._value

    @val.setter
    def val(self, value):
        self._value = value


class _Pars:
    pass


def _plain_ext(**values):
    pars = _Pars()
    for name, value in values.items():
        setattr(pars, name, _Par(value))
    embody = type("Embody", (), {"par": pars})()
    parent = type("Parent", (), {"Embody": embody})()
    owner = type("Owner", (), {"parent": parent})()
    ext_class = _load_convoy_ext_class()
    ext = ext_class.__new__(ext_class)
    ext.ownerComp = owner
    ext._contract_logs = []
    ext._log = lambda message, level="INFO": ext._contract_logs.append(
        (level, message))
    return ext, pars


def test_source_and_nested_convoy_parameter_pages_match():
    source = _convoy_page(_load_yaml(EMBODY_TDN))
    nested = _convoy_page(_load_yaml(ROOT_TDN), nested=True)
    assert nested == source


def test_agreed_controls_have_safe_defaults_and_detailed_help():
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))

    enable_help = rows["Convoyenable"]["help"]
    assert "trusted LAN" in enable_help
    assert "untrusted networks" in enable_help
    assert "does not configure or launch an AI client" in enable_help
    assert "loopback-only" not in enable_help

    assert rows["Convoynodename"]["style"] == "Str"
    assert rows["Convoynodename"].get("default", "") == ""
    # It must rest EMPTY, not carry a baked value. It was briefly a
    # parameter EXPRESSION, and TD stores an expression's last evaluated
    # result beside it -- which shipped one developer's computer name in
    # the release .tox. ConvoyExt fills it per machine at load instead.
    assert rows["Convoynodename"].get("value") in (None, "")
    assert "hostname" in rows["Convoynodename"]["help"]

    assert rows["Convoyremotewake"]["default"] is True
    assert "never wake" in rows["Convoyremotewake"]["help"]
    assert rows["Convoywakegrace"]["default"] == 60

    for name in ("Convoyallowtdpython", "Convoyallowfullshell"):
        assert rows[name]["style"] == "Toggle"
        assert rows[name].get("default", False) is False
        assert "only be enabled locally" in rows[name]["help"]

    quota = rows["Convoyartifactquota"]
    assert quota["style"] == "Int"
    assert quota["default"] == 1024
    for phrase in ("screenshots", "least-recently-used", "Active transfers",
                   "all Convoy-enabled Embody nodes"):
        assert phrase in quota["help"]


def test_status_sequence_has_the_agreed_read_only_columns():
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    sequence = rows["Convoynodes"]
    assert sequence["style"] == "Sequence"
    # Labelled "Convoy Nodes": it lists NODES, and every other
    # parameter in this system is named for what it shows.
    assert sequence["label"] == "Convoy Nodes"
    assert sequence["readOnly"] is True
    assert rows["Convoystatus"]["label"] == "Status"

    expected = ("Nodename", "Ipaddress", "Nodestatus", "Lastseen")
    for name in expected:
        assert rows[name]["sequence"] == "Convoynodes"
        assert rows[name]["readOnly"] is True
    assert set(rows[n]["sequence"] for n in expected) == {"Convoynodes"}


def test_the_single_status_readout_replaces_the_host_field():
    """Convoyid and Convoyhoststatus were removed: a truncated convoy hash, a
    truncated host hash and a process id are not actionable. One Status line
    carries both the node state and any blocking host-app state."""
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    assert "Convoyhoststatus" not in rows
    assert "Convoyid" not in rows
    assert rows["Convoystatus"]["readOnly"] is True
    install_help = rows["Convoyinstallhost"]["help"]
    assert "repair" in install_help.lower()


def test_status_sequence_is_registered_for_release_scrubbing():
    value = _class_assignment(
        EMBODY_EXT, "EmbodyExt", "_TRANSIENT_STATUS_PARS")
    registry = ast.literal_eval(value)
    assert "Convoynodes" in registry["Embody"]
    assert registry["Embody"]["Convoynodes"] is None


def test_danger_gates_are_not_project_config_persisted():
    value = _class_assignment(EMBODY_EXT, "EmbodyExt", "_PERSISTED_PARAMS")
    assert isinstance(value, ast.Call)
    persisted = set(ast.literal_eval(value.args[0]))
    assert "Convoyallowtdpython" not in persisted
    assert "Convoyallowfullshell" not in persisted


def test_saved_danger_projections_reset_fail_closed():
    ext, pars = _plain_ext(Convoyallowtdpython=1, Convoyallowfullshell=True)
    ext._resetUntrustedDangerProjections()
    assert pars.Convoyallowtdpython.eval() == 0
    assert pars.Convoyallowfullshell.eval() == 0
    assert ext._contract_logs and ext._contract_logs[-1][0] == "WARNING"


def test_synthetic_on_request_cannot_create_local_approval():
    ext, pars = _plain_ext(Convoyallowtdpython=1, Convoyallowfullshell=0)
    ext._projecting_policy = False
    ext._policy_busy = False
    ext._session = lambda: {
        "node_id": "n" * 32,
        "policy": {
            "allow_td_python": False,
            "allow_full_shell": False,
            "artifact_quota_mb": 1024,
        },
    }
    calls = []
    ext._beginPolicyCall = lambda action, **request: calls.append(
        (action, request)) or True
    result = ext.LocalDangerGateChanged("Convoyallowtdpython", True)
    assert result == {"ok": True, "pending": True, "enabled": False}
    assert pars.Convoyallowtdpython.eval() == 0
    assert calls == [("policy_begin", {
        "setting": "td_python", "node_id": "n" * 32})]


def test_node_name_override_is_bounded_and_empty_falls_back():
    ext, pars = _plain_ext(Convoynodename="  custom node  ")
    assert ext._nodeName("host", "show") == "custom node"
    pars.Convoynodename.val = ""
    assert ext._nodeName("host", "show") == "host / show"
    pars.Convoynodename.val = "x" * 600
    assert ext._nodeName("host", "show") == "x" * 512


def test_wake_only_route_is_advertised_only_for_perform_mode():
    ext_class = _load_convoy_ext_class()
    endpoint = (47631, "A" * 43)
    assert ext_class._advertisedWakeEndpoint(True, True, endpoint) == endpoint
    assert ext_class._advertisedWakeEndpoint(True, False, endpoint) == (
        None, None)
    assert ext_class._advertisedWakeEndpoint(False, True, endpoint) == (
        None, None)


def test_status_projection_is_the_four_agreed_columns():
    ext_class = _load_convoy_ext_class()
    rows = ext_class._nodeStatusRows({
        "state": "nodes",
        "nodes": [{
            "node_id": "n" * 32,
            "host_id": "h" * 32,
            "node_name": "render / show",
            "status": "online",
            "online": True,
            "controller_count": 3,
            "last_seen_age_s": 65,
        }],
    })
    # Four columns only. controller_count is still consumed from the
    # coalesced directory (never a separate mesh query) -- it simply is not
    # a standing column; convoy_list_controllers is where counts belong.
    assert set(rows[0]) == {"Nodename", "Ipaddress", "Nodestatus", "Lastseen"}
    assert rows[0]["Lastseen"] == "1m ago"


def test_lan_scope_requires_a_new_explicit_consent_marker():
    ext_class = _load_convoy_ext_class()
    assert ext_class.CONSENT_SCOPE == "trusted LAN Convoy mesh"
    source = CONVOY_EXT.read_text(encoding="utf-8")
    assert "local host app only" in source  # legacy migration marker/prose
    assert "convoy_scope_upgrade_required" in source
