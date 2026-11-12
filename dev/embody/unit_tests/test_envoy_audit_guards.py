"""EnvoyExt paths that failed inside their own error handling (audit 2026-11-10).

Each of these raised AttributeError from an `except` block, so the failure it
was written to absorb escaped instead: the Convoy loop check killed the
liveness watchdog chain, and an unreadable docs catalog turned every get_docs
into "Documentation lookup failed". Plus the root claim scope, which matched
nothing. Pure Python: EnvoyExt.py imports only the stdlib at module level.
"""

from __future__ import annotations

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
