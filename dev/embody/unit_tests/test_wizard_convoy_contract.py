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
    wizard = _Wizard(**{"ext_needed": False, "git_missing": False, **values})
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
    # This asserts how the CHOICE is reported; the unsaved-project branch is
    # covered separately. Off-TD there is no `project` global, so pin it.
    logic._projectSaved = lambda: True
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

# -- UNSAVED PROJECT: the save GATE (step 1) ---------------------------
#
# The original failure: dragged the .tox into a NEW network, chose Enable
# Convoy in the wizard, and it silently turned itself back off -- a node
# is identified by its project folder. The first fix warned in text; the
# real fix is structural: an unsaved project gets a SAVE step before all
# others (everything the wizard creates -- .venv, AI config, .embody
# state, the git repo -- lands relative to the project folder), and Next
# is locked until the project is actually saved on disk.

def test_unsaved_project_gets_the_save_gate_first():
    logic = _logic_with(needs_save=True, sel_mode="auto",
                        sel_assistant="none")
    assert logic.spine()[0] == "save"


def test_saved_project_never_shows_the_save_step():
    logic = _logic_with(needs_save=False, sel_mode="auto",
                        sel_assistant="none")
    assert "save" not in logic.spine()


def test_save_step_locks_next_until_the_project_is_saved():
    """Pinned at source level: render() gates Next on _projectSaved()
    for the save step, and click('next') refuses to advance past it --
    the ONE gate in the wizard that a click cannot skip."""
    source = LOGIC.read_text(encoding="utf-8")
    assert "if cur=='save': ok=_projectSaved()" in source
    assert "if cur=='save' and not _projectSaved(): return" in source
    # The save action re-probes the folder-dependent gates so later
    # steps (git, externalize) see the REAL project location.
    assert "_findGitRootSync" in source.split("def _saveProjectNow", 1)[1]


def test_wizard_convoy_hint_no_longer_shouts_about_saving():
    """The save gate guarantees a saved project by the Convoy step; the
    hint is the plain description on saved AND unsaved projects."""
    logic = _load_logic()
    for saved in (True, False):
        logic._projectSaved = lambda s=saved: s
        assert logic._convoyHint() == logic.DEFS["convoy"]["hint"]
    source = LOGIC.read_text(encoding="utf-8")
    assert "SAVE YOUR PROJECT FIRST" not in source


def test_an_actionable_node_state_outranks_the_host_app_line():
    """"Not installed" must never hide "Waiting for project save": installing
    a host app does not fix an unsaved project."""
    src = (LOGIC.parents[1] / "convoy" / "ConvoyExt.py").read_text(
        encoding="utf-8")
    assert "_ACTIONABLE_NODE_TEXTS" in src
    actionable = src.split("_ACTIONABLE_NODE_TEXTS = (", 1)[1].split(")", 1)[0]
    assert "Waiting for project save" in actionable
    # and it must be applied AFTER the host line, so it wins
    assert src.index("_ACTIONABLE_NODE_TEXTS)") > src.index(
        "_BLOCKING_HOST_TEXTS)")


# -- UNSAVED PROJECT: nothing self-initiated writes to disk -----------
#
# A fresh never-saved project has no real folder: everything written
# before the wizard's save step (logs/, .embody/catalog_<build>.json,
# project1/externalizations.tsv, .embody/local+project.json) landed in
# TD's default location and was orphaned by the save (field-reported
# 2026-08-04). Every self-initiated writer now sits behind
# EmbodyExt._projectSavedOnDisk; these pins keep it that way.

def test_every_self_initiated_writer_is_save_gated():
    src = (LOGIC.parents[1] / "EmbodyExt.py").read_text(encoding="utf-8")
    assert "def _projectSavedOnDisk" in src
    log_fn = src.split("def _get_log_file_path", 1)[1].split("def ", 1)[0]
    assert log_fn.index("_projectSavedOnDisk") < log_fn.index("os.makedirs")
    add_fn = src.split("def handleAddition", 1)[1].split(
        "def _handleTDNAddition", 1)[0]
    assert "_projectSavedOnDisk" in add_fn
    tdn_fn = src.split("def _handleTDNAddition", 1)[1].split(
        "\n    def ", 1)[0]
    assert "_projectSavedOnDisk" in tdn_fn
    uh = src.split("def UpdateHandler", 1)[1].split("\n    def ", 1)[0]
    assert "_projectSavedOnDisk" in uh
    pj = src.split("def _writeProjectJson", 1)[1].split("\n    def ", 1)[0]
    assert "_projectSavedOnDisk" in pj


def test_catalog_writes_are_save_gated_and_flushed_after_save():
    cat = (LOGIC.parents[1] / "CatalogManagerExt.py").read_text(
        encoding="utf-8")
    wc = cat.split("def _writeCatalog", 1)[1].split("\n\tdef ", 1)[0]
    assert "_projectSavedOnDisk" in wc and "_deferred_catalog" in wc
    assert wc.index("_projectSavedOnDisk") < wc.index("os.makedirs")
    sent = cat.split("def _writeInflightSentinel", 1)[1].split(
        "\n\tdef ", 1)[0]
    assert "_projectSavedOnDisk" in sent
    assert "def _flushDeferredCatalog" in cat
    # The save is what lands the deferred state: the post-save hook must
    # flush the catalog AND kick the deferred additions sweep.
    exe = (LOGIC.parents[1] / "execute.py").read_text(encoding="utf-8")
    post = exe.split("def onProjectPostSave", 1)[1]
    assert "_flushDeferredCatalog" in post
    assert "Update()" in post


# -- ROUTING: every option group must be WIRED to the click router -----
#
# All wizard clicks route through ONE panelexecute DAT ('clicks') whose
# `panels` pattern enumerates the groups explicitly. v6.0.205 shipped
# grp_save WITHOUT a pattern entry: the save card rendered but clicks
# died silently, leaving the save step's Next permanently locked on a
# fresh project (field-reported on macOS; only "Not now" still worked).
# Handler-level tests cannot catch this (they call click() directly,
# below the panel pipeline), so this pins the wiring itself.

def test_every_option_group_is_wired_to_the_click_router():
    import yaml
    doc = yaml.safe_load(WIZARD_TDN.read_text(encoding="utf-8"))
    clicks = next(o for o in doc["operators"] if o["name"] == "clicks")
    panels = clicks["parameters"]["panels"].split()
    assert "footer/btn_*" in panels
    logic = _load_logic()
    for step, d in logic.DEFS.items():
        g = d.get("g")
        if not g:
            continue
        assert "%s/opt_*" % g in panels, (
            "group %r (step %r) is not in the clicks DAT's panels pattern --"
            " its option cards would render but never respond" % (g, step))


def test_save_action_card_fires_once_per_click():
    """opt_savenow opens the OS save dialog, so it must fire on the press
    edge ONLY -- the release edge would reopen a canceled dialog.
    Selection cards keep firing on both edges (idempotent by design)."""
    clicks_py = (LOGIC.parents[1] / "wizard" / "clicks.py").read_text(
        encoding="utf-8")
    on_to_off = clicks_py.split("def onOnToOff", 1)[1]
    assert 'n!="opt_savenow"' in on_to_off.replace(" ", "")


# -- LAYOUT: every step must FIT the fixed-height panel ----------------
#
# The wizard is a fixed-size verttb stack; hints auto-size to their text
# (_hintHeight). v6.0.204 shipped with the root still at its old height,
# so the three tallest steps pushed Back/Next off the bottom edge. This
# recomputes each step's worst-case stack from wizard.tdn geometry plus
# the REAL hint variants and pins it under the root height.

_CHROME = ("topspacer", "steplabel", "track", "title",
           "divider", "footer", "botspacer")


def test_every_wizard_step_fits_the_panel_height():
    import yaml
    doc = yaml.safe_load(WIZARD_TDN.read_text(encoding="utf-8"))
    ops = {o["name"]: o for o in doc["operators"]}

    def height(name):
        h = ops[name]["parameters"].get("h")
        assert h is not None, "wizard.tdn lost the explicit height of %r" % name
        return h

    root_h = doc["parameters"]["h"]
    spacing = doc["parameters"]["spacing"]
    chrome = sum(height(n) for n in _CHROME)
    # The fill spacer must be IN the align flow: it is what pins the
    # footer to the bottom on short steps.
    assert "display" not in ops["spacer"]["parameters"], \
        "spacer must not carry a display override (default-on)"

    logic = _load_logic()
    variants = {step: [d["hint"]] for step, d in logic.DEFS.items()}
    variants["permissions"].append(
        logic.DEFS["permissions"]["hint"]
        + "\nA settings.local.json already exists -- Embody edits only its"
          " Envoy entries, keeping the rest.")
    convoy_only = _logic_with(sel_assistant="none", sel_convoy="enable")
    variants["footprint"].append(convoy_only._footprintHint())
    busy = _logic_with(sel_mode="advanced", sel_assistant="other",
                       sel_client="windsurf", git_missing=True,
                       sel_git="gitinit", ext_needed=True,
                       sel_externalize="full", sel_convoy="enable")
    busy._projectSaved = lambda: True
    variants["summary"].append(busy._recap())

    for step, d in logic.DEFS.items():
        grp = d.get("g")
        grp_h = height(grp) if grp else 0
        visible = len(_CHROME) + 2 + (1 if grp else 0)  # + hint + spacer
        gaps = (visible - 1) * spacing
        for text in variants[step]:
            need = chrome + gaps + logic._hintHeight(text) + grp_h
            assert need <= root_h, (
                "step %r needs %dpx but the wizard panel is %dpx --"
                " Back/Next would clip off the bottom" % (step, need, root_h))


def test_recap_reports_convoy_plainly_behind_the_save_gate():
    """The summary can promise "Convoy: enabled" unconditionally now:
    the save gate (step 1) means no unsaved project ever reaches it."""
    logic = _logic_with(sel_mode="auto", sel_assistant="none",
                        sel_convoy="enable")
    logic._projectSaved = lambda: False   # even then: the gate owns this
    recap = logic._recap()
    assert "Convoy: enabled" in recap
    assert "SAVE THE PROJECT" not in recap
