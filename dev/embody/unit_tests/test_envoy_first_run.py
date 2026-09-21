"""Off-TD tests for the first-run Envoy consent decision.

The bug these pin: a fresh CLONE has no .embody/config.json, so
restore_settings takes its fresh-clone branch, calls adopt_committed_envoy
(which turns Envoy on from the committed declaration) and returns False.
verify() read that False as "genuinely fresh install" and wrote
Envoyenable back to False -- undoing the adopt three lines later.

That scrub is the one write on this path that parexec actually sees:
_init_complete is stored by the time it lands, so the deferred callback
runs Stop() ('Envoy disabled', Envoystatus='Disabled'), and because
Envoyenable is persisted, parexec's tail then bakes `Envoyenable: false`
into config.json -- which restore_settings replays on every later open.
The clone never gets Envoy, and the watchdog cannot rescue it because it
idles whenever the par reads False.

verify() itself needs a live TouchDesigner session, so the decision lives
in embody_admin as a pure function and is pinned here. Both halves of the
coupling it depends on (init writes False at frame 0; verify runs after)
are pinned by source scan.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
EMBODY_SRC = REPO / "dev" / "embody" / "Embody"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


admin = _load("first_run_embody_admin", EMBODY_SRC / "embody_admin.py")


class _Par:
    def __init__(self, val):
        self.val = val

    def eval(self):
        return self.val


class _Pars:
    """TD pars intercept assignment: comp.par.X = v writes the VALUE, it
    does not rebind the name. A fake that misses this lets a test pass while
    the product would have raised."""

    def __setattr__(self, name, value):
        par = self.__dict__.get(name)
        if isinstance(par, _Par):
            par.val = value
        else:
            self.__dict__[name] = value


class _My:
    def __init__(self, enabled):
        self.par = _Pars()
        self.par.Envoyenable = _Par(enabled)

    def __str__(self):
        return "/project1/Embody"


class _Ext:
    """The slice of EmbodyExt the decision reads."""

    def __init__(self, enabled=False, suppress=False, pending=False):
        self.my = _My(enabled)
        self._suppress = suppress
        self._restoring_settings = False
        self.logged = []
        if pending:
            self._pending_envoy_prompt = True

    def _suppressDialogs(self):
        return self._suppress

    def Log(self, msg, level="INFO"):
        self.logged.append((level, msg))


# --------------------------------------------------------------------------
# the decision itself
# --------------------------------------------------------------------------

def test_an_enable_that_already_happened_is_honoured():
    assert admin.envoy_consent_decision(_Ext(enabled=True)) == "honour"


def test_honour_wins_even_while_dialogs_are_suppressed():
    """Suppression governs whether we may ASK, not whether we may obey an
    answer already given. A save or a test run must not scrub the par."""
    ext = _Ext(enabled=True, suppress=True)
    assert admin.envoy_consent_decision(ext) == "honour"


def test_a_genuinely_fresh_install_is_prompted():
    assert admin.envoy_consent_decision(_Ext()) == "prompt"


def test_a_suppressed_fresh_install_stays_quiet():
    assert admin.envoy_consent_decision(_Ext(suppress=True)) == "quiet"


def test_an_already_queued_prompt_is_not_queued_twice():
    assert admin.envoy_consent_decision(_Ext(pending=True)) == "quiet"


# --------------------------------------------------------------------------
# the path that actually produced the bug
# --------------------------------------------------------------------------

def test_the_clone_adopt_path_ends_in_honour(tmp_path, monkeypatch):
    """adopt_committed_envoy turns Envoy on and returns; the decision made
    immediately after it must not undo that."""
    ext = _Ext()
    monkeypatch.setattr(admin, "read_envoy_enabled", lambda e: True)
    adopted = admin.adopt_committed_envoy(ext, kick_envoy=False)
    assert adopted is True
    assert ext.my.par.Envoyenable.val is True
    assert admin.envoy_consent_decision(ext) == "honour"


def test_a_machine_with_no_declaration_is_still_prompted(monkeypatch):
    monkeypatch.setattr(admin, "read_envoy_enabled", lambda e: False)
    ext = _Ext()
    assert admin.adopt_committed_envoy(ext, kick_envoy=False) is False
    assert admin.envoy_consent_decision(ext) == "prompt"


def test_adopt_never_fights_a_local_opt_out(monkeypatch):
    """A machine that already holds an opinion keeps it -- adopt is only for
    a clone with no config.json at all."""
    monkeypatch.setattr(admin, "read_envoy_enabled", lambda e: True)
    ext = _Ext(enabled=True)
    assert admin.adopt_committed_envoy(ext, kick_envoy=False) is False


# --------------------------------------------------------------------------
# the coupling the discriminator rests on
# --------------------------------------------------------------------------

def test_init_still_scrubs_envoyenable_at_frame_zero():
    """The decision is only exact because init() left the par False before
    verify() ever looks at it. Replace that write with a boot guard and a
    baked-True release .tox would read as consent."""
    src = (EMBODY_SRC / "execute.py").read_text(encoding="utf-8")
    body = src.split("def init(", 1)[1].split("\ndef ", 1)[0]
    assert "parent.Embody.par.Envoyenable = False" in body


def test_verify_runs_after_init_on_the_fresh_install_path():
    src = (EMBODY_SRC / "execute.py").read_text(encoding="utf-8")
    body = src.split("def onCreate(", 1)[1].split("\ndef ", 1)[0]
    assert "init()" in body
    assert "verify()" in body
    assert body.index("init()") < body.index("verify()")


def test_the_fresh_branch_no_longer_scrubs_unconditionally():
    """The regression guard: a bare `Envoyenable = False` reachable without
    first checking the live value is the bug coming back."""
    src = (EMBODY_SRC / "EmbodyExt.py").read_text(encoding="utf-8")
    body = src.split("def verify(self)", 1)[1].split("\n    def ", 1)[0]
    assert "envoy_consent_decision" in body, (
        "verify() must take the decision through the pinned helper")
    scrub = body.find("self.my.par.Envoyenable = False")
    guard = body.find("decision == 'prompt'")
    assert scrub > 0 and guard > 0
    assert guard < scrub, "the scrub must sit inside the prompt branch"


def test_the_honour_branch_starts_the_server():
    """The enable's own onValueChange was swallowed by parexec's
    _init_complete guard, so nothing else will start it."""
    src = (EMBODY_SRC / "EmbodyExt.py").read_text(encoding="utf-8")
    body = src.split("def verify(self)", 1)[1].split("\n    def ", 1)[0]
    honour = body.split("decision == 'honour'", 1)[1].split("elif", 1)[0]
    assert "Envoy.Start()" in honour


def test_envoyenable_is_persisted_so_a_scrub_would_be_permanent():
    """Why the bug outlived the session: parexec's tail writes the par to
    config.json, and restore_settings replays it on every later open."""
    src = (EMBODY_SRC / "EmbodyExt.py").read_text(encoding="utf-8")
    block = src.split("_PERSISTED_PARAMS", 1)[1][:2000]
    assert "'Envoyenable'" in block


def test_the_committed_declaration_is_strict_json_true(tmp_path):
    """adopt reads consent from committed metadata, so a truthy string must
    not count -- it would disagree with the writer's own idempotency check.
    """
    class _FileExt(_Ext):
        def __init__(self, entry):
            super().__init__()
            self._root = tmp_path
            path = tmp_path / ".embody"
            path.mkdir(exist_ok=True)
            (path / "project.json").write_text(
                json.dumps({admin.ENVOY_KEY: entry}), encoding="utf-8")

        def _findProjectRoot(self):
            return self._root

    assert admin.read_envoy_enabled(_FileExt({"enabled": True})) is True
    assert admin.read_envoy_enabled(_FileExt({"enabled": "true"})) is False
    assert admin.read_envoy_enabled(_FileExt({})) is False
