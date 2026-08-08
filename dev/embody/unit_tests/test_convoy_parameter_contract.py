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
    # Builds a FRESH, sys.modules-unregistered module per call -- the
    # _name_ext fakes inject ParMode/project/socket into the loaded
    # module's globals and rely on this isolation; do not add caching.
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
    ext._log = lambda message, level="INFO", details=None: (
        ext._contract_logs.append((level, message)))
    return ext, pars


def test_forget_offline_nodes_pulse_is_present_and_honest():
    """The manual cleanup path: a Pulse right after the nodes sequence,
    whose help states the enumerating confirmation and the consequence
    (new identity, TD Python approval reset) -- the informed-consent
    contract the button was designed around."""
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    par = rows["Convoyforgetoffline"]
    assert par["style"] == "Pulse"
    assert par["label"] == "Forget Offline Nodes..."
    help_text = par["help"].lower()
    for phrase in ("confirmation", "names", "new identity",
                   "td python approval resets",
                   # The CONTRACT, in the words the daemon now enforces:
                   # only an unfinished delivery keeps a row. The old
                   # phrasing ("unresolved jobs") described the predicate
                   # that CAUSED the 2026-08-06 field bug, and pinning it
                   # here is part of what kept it in front of users after
                   # the behaviour had changed underneath.
                   "has not finished"):
        assert phrase in help_text, phrase
    assert "unresolved jobs" not in help_text, \
        "the pre-6.0.219 contract must not be described to the user"


def test_the_convoy_comp_carries_its_global_shortcut():
    """External call sites address the extension as op.Convoy.ext.ConvoyExt
    -- the shortcut is the API surface, so losing it breaks every example
    in the docs (and the wizard's node-name fill)."""
    document = _load_yaml(EMBODY_TDN)
    convoy = next(row for row in document["operators"]
                  if row.get("name") == "convoy")
    assert convoy["parameters"].get("opshortcut") == "Convoy"


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
    # the release .tox. ConvoyExt fills it per machine at load instead,
    # and only once the project is saved (see the node-name fill tests).
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

# -- RELEASE GATE: nothing machine-specific may ship -------------------
#
# This is the test that would have caught v6.0.183, which shipped this
# developer's computer name, host id and convoy id, AND shipped Convoy
# switched ON -- enabling a LAN listener on every machine that installed it
# without anyone opting in. It was found by hand, by expanding the released
# .tox. Never again by hand: Embody.tdn is the tracked source every release
# .tox is built from, so asserting on it is the same guarantee, and it runs
# anywhere without TouchDesigner.

_MACHINE_MARKERS = ("192.168.", "10.0.", "TEC-", "cv_", "cvfp1-",
                    "C:/Users/", r"C:\Users")


def test_release_source_carries_no_machine_identity():
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    for name, row in rows.items():
        value = row.get("value")
        if not isinstance(value, str):
            continue
        for marker in _MACHINE_MARKERS:
            assert marker not in value, (
                "%s ships %r -- a hostname, address, convoy/host id or local "
                "path must never bake into the release" % (name, value))


def test_release_source_ships_convoy_disabled():
    """Consent, not state. A released .tox that arrives with Convoy ON opens
    a trusted-LAN listener on a stranger's machine before they have agreed to
    anything. Absent (TD omits its own default) or an explicit off both pass."""
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    enable = rows["Convoyenable"]
    assert enable.get("value") in (None, False, 0),         "Convoyenable must ship OFF"
    assert enable.get("default") in (None, False, 0)


def test_release_source_ships_an_empty_node_name_and_status():
    """Node Name auto-fills per machine at load; a baked value ships one
    developer's computer name. Status is a live readout, not a shipped fact."""
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    assert rows["Convoynodename"].get("value") in (None, "")
    assert rows["Convoystatus"].get("value") in (None, "Disabled")


def test_release_source_ships_no_node_rows():
    """The Convoy Nodes sequence is a runtime projection. Shipping rows would
    leak another machine's names, addresses and presence."""
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    for name in ("Nodename", "Ipaddress", "Nodestatus", "Lastseen"):
        assert rows[name].get("value") in (None, ""),             "%s must ship empty" % name


def test_release_source_keeps_the_danger_gates_off():
    """A stale On in a saved project or a clone must never grant these."""
    rows = _by_name(_convoy_page(_load_yaml(EMBODY_TDN)))
    for name in ("Convoyallowtdpython", "Convoyallowfullshell"):
        assert rows[name].get("value") in (None, False, 0)
        assert rows[name].get("default", False) in (None, False, 0)


# -- wedged-slot recovery (the Mac first-install wedge, 2026-08-04) -----
#
# A drain chain that dies -- by exception, or a stale-instance misfire --
# used to orphan its busy flag forever: every later host/policy action
# answered 'still in progress' over a call that finished within seconds,
# until a TD restart. These pin the recovery paths.


def test_wedged_policy_slot_with_parked_result_is_delivered():
    ext, _ = _plain_ext()
    ext._policy_busy = True
    ext._policy_result = {"_gen": 7, "_action": "policy_refresh",
                          "request": {}, "result": {"state": "x"}}
    delivered = []
    ext._finishPolicyCall = lambda action, result, request: delivered.append(
        (action, result, request))
    assert ext._policyBusyBlocked() is False
    assert delivered == [("policy_refresh", {"state": "x"}, {})]
    assert ext._policy_busy is False and ext._policy_result is None
    assert ext._contract_logs[-1][0] == "WARNING"


def test_wedged_policy_slot_past_budget_recovers():
    ext, _ = _plain_ext()
    ext._policy_busy = True
    ext._policy_result = None
    ext._policy_busy_since = 0.0
    assert ext._policyBusyBlocked() is False
    assert ext._policy_busy is False
    assert ext._contract_logs[-1][0] == "WARNING"


def test_live_policy_slot_still_blocks():
    ext, _ = _plain_ext()
    ext._policy_busy = True
    ext._policy_result = None
    ext._policy_busy_since = 10 ** 12  # just started, in effect
    assert ext._policyBusyBlocked() is True
    assert ext._policy_busy is True


def test_policy_poll_crash_recovers_the_slot():
    ext, _ = _plain_ext()
    ext._staleInstance = lambda: False
    ext._policy_busy = True
    ext._policy_result = {"_gen": 3, "_action": "policy_refresh",
                          "request": {}, "result": {"state": "x"}}

    def boom(action, result, request):
        raise RuntimeError("finish blew up")

    ext._finishPolicyCall = boom
    ext._pollPolicyCall(3, 0)
    assert ext._policy_busy is False and ext._policy_result is None
    assert ext._contract_logs[-1][0] == "ERROR"


def test_stale_policy_poll_clears_its_slot():
    ext, _ = _plain_ext()
    ext._staleInstance = lambda: True
    ext._policy_busy = True
    ext._policy_result = {"_gen": 3}
    ext._pollPolicyCall(3, 0)
    assert ext._policy_busy is False and ext._policy_result is None


def test_wedged_host_slot_with_parked_result_is_delivered():
    ext, _ = _plain_ext()
    ext._host_busy = True
    ext._host_action = "install"
    ext._host_result = {"_gen": 2, "result": {"ok": True}}
    delivered = []
    ext._finishHost = lambda action, result: delivered.append(
        (action, result))
    assert ext._recoverWedgedHostSlot() is True
    assert delivered == [("install", {"ok": True})]
    assert ext._host_busy is False and ext._host_result is None
    assert ext._contract_logs[-1][0] == "WARNING"


def test_wedged_host_slot_past_budget_recovers():
    ext, _ = _plain_ext()
    ext._host_busy = True
    ext._host_result = None
    ext._host_busy_since = 0.0
    assert ext._recoverWedgedHostSlot() is True
    assert ext._host_busy is False


def test_live_host_slot_still_blocks():
    ext, _ = _plain_ext()
    ext._host_busy = True
    ext._host_result = None
    ext._host_busy_since = 10 ** 12
    assert ext._recoverWedgedHostSlot() is False
    assert ext._host_busy is True


# -- Node Name fill: never bake the unsaved placeholder ----------------
#
# _ensureNodeName runs at extension init, which on a fresh install is
# BEFORE the wizard's save step. v6.0.206 and earlier filled the Node
# Name parameter immediately, baking `hostname / NewProject.1` -- and a
# non-empty value reads as a user override forever, so the node kept
# registering under the throwaway name after the project was saved as
# e3 (field-reported 2026-08-04 on macOS).


class _NamePar(_Par):
    def __init__(self, value, mode="CONSTANT"):
        super().__init__(value)
        self.mode = mode


def _name_ext(value, saved, project_name, hostname="TEC-MBA.local",
              mode="CONSTANT"):
    ext, pars = _plain_ext()
    par = _NamePar(value, mode=mode)
    pars.Convoynodename = par
    ext._savedToe = lambda: saved
    g = type(ext)._ensureNodeName.__globals__
    g["ParMode"] = type("ParMode", (), {"CONSTANT": "CONSTANT"})
    g["project"] = type("Project", (), {"name": project_name})()
    g["socket"] = type("Socket", (), {
        "gethostname": staticmethod(lambda: hostname)})()
    return ext, par


def test_node_name_fill_waits_for_a_saved_project():
    ext, par = _name_ext("", saved=None, project_name="NewProject.1.toe")
    ext._ensureNodeName()
    assert par.val == ""


def test_node_name_fill_stamps_the_saved_toe_stem():
    ext, par = _name_ext("", saved="/work/e3.toe", project_name="e3.toe")
    ext._ensureNodeName()
    assert par.val == "TEC-MBA.local / e3"


def test_baked_default_project_placeholder_is_healed():
    for baked in ("TEC-MBA.local / NewProject.1",
                  "TEC-MBA.local / NewProject",
                  "TEC-MBA.local / NewProject.12"):
        ext, par = _name_ext(baked, saved="/work/e3.toe",
                             project_name="e3.toe")
        ext._ensureNodeName()
        assert par.val == "TEC-MBA.local / e3", baked


def test_baked_placeholder_on_a_still_unsaved_project_waits():
    """The heal obeys the same gate as the fill: no rewrite until the
    project is actually on disk."""
    ext, par = _name_ext("TEC-MBA.local / NewProject.1", saved=None,
                         project_name="NewProject.1.toe")
    ext._ensureNodeName()
    assert par.val == "TEC-MBA.local / NewProject.1"


def test_user_override_is_never_clobbered():
    for value in ("My Booth Node",
                  "OTHER-HOST / NewProject.1",   # not THIS machine's stamp
                  "TEC-MBA.local / NewProject.1 (stage)"):
        ext, par = _name_ext(value, saved="/work/e3.toe",
                             project_name="e3.toe")
        ext._ensureNodeName()
        assert par.val == value, value


def test_expression_mode_node_name_is_untouched():
    ext, par = _name_ext("TEC-MBA.local / NewProject.1",
                         saved="/work/e3.toe", project_name="e3.toe",
                         mode="EXPRESSION")
    ext._ensureNodeName()
    assert par.val == "TEC-MBA.local / NewProject.1"
