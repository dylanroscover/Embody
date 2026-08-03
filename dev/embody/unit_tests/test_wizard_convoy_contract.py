"""Plain-Python contracts for Convoy's setup-wizard routing.

The wizard DAT intentionally has no module-level TouchDesigner access, so its
decision spine and recap can be exercised off-TD with one small owner fake.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


LOGIC = (Path(__file__).resolve().parents[1]
         / "Embody" / "wizard" / "logic.py")
ENVOY_EXT = LOGIC.parents[1] / "EnvoyExt.py"
WIZARD_TDN = LOGIC.parents[1] / "wizard.tdn"


def _load_logic():
    spec = importlib.util.spec_from_file_location(
        "wizard_convoy_contract_logic", LOGIC)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_envoy_ext():
    spec = importlib.util.spec_from_file_location(
        "wizard_convoy_contract_envoy", ENVOY_EXT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.EnvoyExt


class _Wizard:
    def __init__(self, **values):
        self.values = values

    def fetch(self, name, default=None, search=False):
        del search
        return self.values.get(name, default)

    def op(self, path):
        # spine() only needs to know that the externalize group exists.
        return object() if path == "grp_externalize" else None


def _logic_with(**values):
    logic = _load_logic()
    wizard = _Wizard(ext_needed=False, git_missing=False, **values)
    logic._w = lambda: wizard
    return logic


def test_convoy_step_is_independent_of_ai_assistant_choice():
    logic = _logic_with(sel_mode="auto", sel_assistant="none",
                        sel_convoy="disable")
    assert "convoy" in logic.spine()


def test_advanced_convoy_only_setup_discloses_internal_runtime_footprint():
    logic = _logic_with(sel_mode="advanced", sel_assistant="none",
                        sel_convoy="enable")
    assert "footprint" in logic.spine()
    hint = logic._footprintHint()
    assert "internal local Envoy command service" in hint
    assert "does not generate AI-client config" in hint


def test_none_assistant_recap_still_reports_convoy_choice():
    logic = _logic_with(sel_mode="auto", sel_assistant="none",
                        sel_convoy="enable")
    recap = logic._recap()
    assert "AI assistant: off" in recap
    assert "Convoy: enabled" in recap
    assert "externalization only" not in recap


def test_only_explicit_none_suppresses_ai_client_configuration():
    envoy_ext = _load_envoy_ext()
    assert envoy_ext._shouldConfigureAIClient("none") is False
    assert envoy_ext._shouldConfigureAIClient(" NONE ") is False
    assert envoy_ext._shouldConfigureAIClient("claudecode") is True
    assert envoy_ext._shouldConfigureAIClient("") is True


def test_none_option_copy_does_not_claim_convoy_has_no_server():
    source = WIZARD_TDN.read_text(encoding="utf-8")
    assert "None - no AI assistant" in source
    assert "Convoy can still use its internal command service" in source
    assert "No .venv, server, or config" not in source
