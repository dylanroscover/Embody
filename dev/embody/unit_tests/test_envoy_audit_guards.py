"""EnvoyExt paths that failed inside their own error handling (audit 2026-11-10).

Each of these raised AttributeError from an `except` block, so the failure it
was written to absorb escaped instead: the Convoy loop check killed the
liveness watchdog chain, and an unreadable docs catalog turned every get_docs
into "Documentation lookup failed". Plus the root claim scope, which matched
nothing, and externalize_op, which answered success while Embody was Disabled
and wrote nothing (issue #194). Pure Python: EnvoyExt.py and envoy_ops.py
import only the stdlib at module level.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_EMBODY = Path(__file__).resolve().parents[1] / "Embody"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, _EMBODY / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_ENVOY = _load("audit_guard_envoy", "EnvoyExt.py")


def test_convoy_loop_check_failure_is_logged_not_raised():
    """_watchdogTick calls this outside any try and before it reschedules:
    an exception here ends the watchdog until the next reinit."""
    logged = []

    def missing_convoy(_name):
        raise RuntimeError('convoy COMP unavailable')

    fake = SimpleNamespace(
        ownerComp=SimpleNamespace(op=missing_convoy),
        _log=lambda message, level='INFO': logged.append((level, message)))
    _ENVOY.EnvoyExt._checkConvoyLoop(fake)
    assert logged and logged[0][0] == 'DEBUG'
    assert 'Convoy loop check skipped' in logged[0][1]


def test_unreadable_docs_catalog_degrades_to_an_empty_index(
        tmp_path, monkeypatch):
    """The docstring promises "any failure yields {}"; EnvoyMCPServer has no
    _log, so the handler itself raised. Worker code logs through the buffer."""
    build = '099.2025.1'
    (tmp_path / '.embody').mkdir()
    (tmp_path / '.embody' / f'catalog_{build}.json').write_text(
        '{not json', encoding='utf-8')
    monkeypatch.setattr(sys, '_envoy_repo_root', str(tmp_path), raising=False)
    fake = SimpleNamespace(_docs_state={'build': build})
    before = len(_ENVOY._WORKER_LOG_LINES)
    index = _ENVOY.EnvoyMCPServer._docsDefaultsIndex(fake)
    assert index == {}
    assert fake._docs_state['defaults'] == {}
    lines = list(_ENVOY._WORKER_LOG_LINES)[before:]
    assert any('Docs catalog unreadable' in text for _level, text in lines)


_OPS = _load("audit_guard_envoy_ops", "envoy_ops.py")


@contextlib.contextmanager
def _no_dialogs():
    yield


def _externalize(status='Enabled', perform=False, saved=True, writes=True):
    """envoy_ops.externalize_op('/project1') on a fake Embody whose Update()
    behaves like the real one: a no-op while Disabled or in Perform Mode.
    Returns (result, the COMP)."""
    written = ['']
    comp = SimpleNamespace(
        family='COMP', type='base', path='/project1', tags=set(),
        par=SimpleNamespace(externaltox=SimpleNamespace(eval=lambda: written[0])),
        fetch=lambda *args, **kwargs: '')

    def tag(target, value):
        target.tags.add(value)
        return True

    def update():
        if status != 'Disabled' and not perform and writes:
            written[0] = 'project1.tox'

    embody_ext = SimpleNamespace(
        _performMode=perform, _isInsideAnnotate=lambda target: False,
        _tdxnTags=lambda: ('tdxn', 'tdn'), _tagRefusal=lambda target, value: None,
        applyTagToOperator=tag, _projectSavedOnDisk=lambda: saved,
        _getStrategyFilePath=lambda path, strategy: '')
    _OPS.op = SimpleNamespace(Embody=SimpleNamespace(
        par=SimpleNamespace(Status=SimpleNamespace(eval=lambda: status),
                            Tdxntag=SimpleNamespace(eval=lambda: 'tdxn')),
        ext=SimpleNamespace(Embody=embody_ext,
                            TDXN=SimpleNamespace(suppressLockedDialogs=_no_dialogs)),
        Update=update))
    ext = SimpleNamespace(_resolve_op=lambda path: comp)
    return _OPS.externalize_op(ext, '/project1'), comp


def test_externalize_op_reports_the_file_it_wrote():
    result, comp = _externalize()
    assert result == {'success': True, 'path': '/project1', 'tag': 'tox',
                      'file': 'project1.tox'}
    assert comp.tags == {'tox'}


def test_externalize_op_refuses_while_embody_is_disabled():
    """Issue #194: a bootstrapped project is Disabled until someone enables
    it, Update() returns at once, and the tool answered success, file ''."""
    result, comp = _externalize(status='Disabled')
    assert 'success' not in result
    assert result['error_code'] == 'envoy.embody.disabled'
    assert 'par.Update.pulse()' in result['error']
    assert comp.tags == set()          # refused before anything was tagged


def test_externalize_op_refuses_in_perform_mode():
    result, comp = _externalize(perform=True)
    assert 'success' not in result
    assert result['error_code'] == 'envoy.embody.perform_mode'
    assert comp.tags == set()


def test_externalize_op_fails_when_a_saved_project_wrote_nothing():
    result, _comp = _externalize(writes=False)
    assert 'success' not in result
    assert result['error_code'] == 'envoy.embody.not_written'


def test_externalize_op_still_defers_on_a_never_saved_project():
    result, _comp = _externalize(saved=False, writes=False)
    assert result['success'] is True and result['deferred'] is True


def test_externalize_refusals_draw_no_unrelated_recovery_hint():
    """The codes ride on the envelope; the wording must not trip another
    rule's regex and hand the agent the wrong advice."""
    for kwargs in ({'status': 'Disabled'}, {'perform': True}, {'writes': False}):
        result, _comp = _externalize(**kwargs)
        assert _ENVOY._error_code_for(result['error']) == _ENVOY._ERROR_CODE_FALLBACK


def test_root_scope_overlaps_every_op_path():
    """claim_scope accepts '/', but '/a'.startswith('//') is False, so a
    root claim protected nothing and root-scoped calls drew no advisory."""
    assert _ENVOY._scope_overlaps('/', '/a')
    assert _ENVOY._scope_overlaps('/a/b', '/')
    assert _ENVOY._scope_overlaps('/', '/')
    # the segment rule and the non-path scopes are unchanged
    assert _ENVOY._scope_overlaps('/a', '/a/b')
    assert not _ENVOY._scope_overlaps('/a/b', '/a/bc')
    assert not _ENVOY._scope_overlaps('/', 'file:dev/x.py')
    assert not _ENVOY._scope_overlaps('project:tests', '/')
