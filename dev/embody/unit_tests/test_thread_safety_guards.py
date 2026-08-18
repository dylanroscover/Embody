"""Runtime thread-safety guards from the 2026-08-17 Derivative advisory.

Two guards, both TD-import-free by construction and therefore testable on
the same windows+macos matrix as the other pure suites:

- ConvoyExt._submitSiblingApi refuses off-main callers. The sibling API
  funnel arms td.run() polls, and worker-side run() silently corrupts TD
  state (it does not raise), so the documented main-thread contract is
  enforced at the funnel: a wrong-thread call gets an error dict instead
  of a latent corruption.
- EnvoyExt's worker-log buffer (_queueWorkerLog / _WORKER_LOG_LINES):
  worker threads buffer diagnostics instead of print()ing into TD's
  Textport-backed stdout; the buffer is bounded and drained main-thread.

The guard does `import td` explicitly (the bare name is not reliably
bound in an extension namespace); under pytest that import fails, and the
failure IS the passthrough case (no td module = not inside TD = nothing
to corrupt). The refusal branch is exercised by planting a fake `td`
module in sys.modules, which is exactly what the guard's import resolves
inside TD.
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


_CONVOY = _load("thread_guard_convoy", "convoy/ConvoyExt.py")
_ENVOY = _load("thread_guard_envoy", "EnvoyExt.py")

_SENTINEL = object()


class _Stub:
    """The minimum of ConvoyExt the guard path can touch."""

    _apiError = staticmethod(_CONVOY.ConvoyExt._apiError)

    def _apiNewRequest(self, kind, callback):
        return None, _SENTINEL


def _submit():
    return _CONVOY.ConvoyExt._submitSiblingApi(_Stub(), 'ping', {})


def _plant_td(is_main):
    sys.modules['td'] = SimpleNamespace(isMainThread=lambda: is_main)


def _clear_td():
    sys.modules.pop('td', None)


# --- ConvoyExt._submitSiblingApi main-thread gate ------------------------

def test_off_main_caller_is_refused_with_a_handle_shaped_result():
    """The refusal mirrors the request_capacity handle (state 'failed',
    error under 'result', request_id present-but-None) so every documented
    consumer access pattern survives without a KeyError."""
    _plant_td(is_main=False)
    try:
        result = _submit()
    finally:
        _clear_td()
    assert isinstance(result, dict)
    assert result['state'] == 'failed'
    assert result['request_id'] is None
    assert result['kind'] == 'ping'
    assert result['result']['ok'] is False
    assert result['result']['reason'] == 'wrong_thread'


def test_main_thread_caller_passes_the_guard():
    _plant_td(is_main=True)
    try:
        result = _submit()
    finally:
        _clear_td()
    assert result is _SENTINEL


def test_missing_td_module_assumes_main_thread():
    """Import failure = not inside TouchDesigner = nothing to corrupt."""
    _clear_td()
    assert _submit() is _SENTINEL


def test_broken_td_module_fails_open():
    """A td that imports but misbehaves must never break the API surface;
    the guard fails open rather than raising out of _submitSiblingApi."""
    sys.modules['td'] = SimpleNamespace()  # no isMainThread attribute
    try:
        assert _submit() is _SENTINEL
    finally:
        _clear_td()


# --- EnvoyExt worker-log buffer ------------------------------------------

def test_queue_worker_log_appends_level_and_message():
    _ENVOY._WORKER_LOG_LINES.clear()
    _ENVOY._queueWorkerLog('socket refused')
    _ENVOY._queueWorkerLog('shutting down', level='INFO')
    assert list(_ENVOY._WORKER_LOG_LINES) == [
        ('WARNING', 'socket refused'),
        ('INFO', 'shutting down'),
    ]
    _ENVOY._WORKER_LOG_LINES.clear()


def test_queue_worker_log_coerces_non_string_messages():
    _ENVOY._WORKER_LOG_LINES.clear()
    _ENVOY._queueWorkerLog(ValueError('boom'))
    level, message = _ENVOY._WORKER_LOG_LINES.popleft()
    assert level == 'WARNING'
    assert message == 'boom'
    _ENVOY._WORKER_LOG_LINES.clear()


def test_worker_log_buffer_is_bounded():
    _ENVOY._WORKER_LOG_LINES.clear()
    for i in range(500):
        _ENVOY._queueWorkerLog(f'line {i}')
    assert len(_ENVOY._WORKER_LOG_LINES) == _ENVOY._WORKER_LOG_LINES.maxlen
    # Oldest lines fall off the front; the newest survive.
    assert _ENVOY._WORKER_LOG_LINES[-1] == ('WARNING', 'line 499')
    _ENVOY._WORKER_LOG_LINES.clear()
