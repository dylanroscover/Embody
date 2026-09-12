"""Dual-runner shim: run the TD-import-free bridge suite under plain
pytest (the A-51 GitHub Actions windows/macos matrix) while staying
byte-compatible with the in-TD TestRunnerExt runner.

Inside TouchDesigner this file is INERT: TestRunnerExt discovers only
test_*.py modules and never imports conftest.py. Under pytest, it
injects -- before collection -- the only two TD globals the bridge
suite's header touches:

  - ``project.folder`` (the directory holding the .toe, i.e. dev/)
  - ``op.unit_tests.op('TestRunnerExt').module`` (the runner module the
    header imports EmbodyTestCase from)

The EmbodyTestCase stand-in mirrors the runner's custom asserts
byte-for-byte and turns every TD-backed fixture (``embody``,
``embody_ext``, ``sandbox``) into a clean SkipTest -- pure tests run on
any machine and any CI runner; TD-bound tests skip loudly instead of
erroring. The macOS runner is the point: the darwin quit/launch paths
and the pgrep-based find_all_td_pids tests are skip-only on the Windows
dev box and landed flagged "unverified on mac" (commit e28b73e).
"""

import builtins
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

import pytest


# The interpreter this suite is CONTRACTED to run on: TouchDesigner's own.
# Local pytest on a different Python does not test what ships -- it silently
# exercises an interpreter no user and no CI leg has. The failure is not
# loud either: on 3.9 six embody_git tests failed because
# Path.write_text(newline=) is 3.10+, the TypeError was swallowed by a broad
# except, and the helper just returned False (field 2026-08-22).
#
# KEEP THIS IN LOCK-STEP WITH TD. Two tests enforce it and will fail the
# moment it drifts -- see test_version_sync:
#   - TD's live sys.version_info must equal this tuple
#   - .github/workflows/bridge-tests.yml python-version must equal it
TD_PYTHON_TARGET = (3, 11)


def _require_td_python():
    """Refuse to run on an interpreter TouchDesigner does not ship.

    Module level, NOT a pytest_configure hook: this file already
    defines pytest_configure further down, and a second def would
    simply replace it (which is exactly what happened first try).
    """
    running = sys.version_info[:2]
    if running == TD_PYTHON_TARGET:
        return
    want = '%d.%d' % TD_PYTHON_TARGET
    have = '%d.%d' % running
    raise SystemExit(
        "\nThis suite must run on Python %s -- the version TouchDesigner "
        "ships and CI pins.\nYou are on Python %s (%s).\n\n"
        "Results from another interpreter are misleading: they exercise "
        "code paths\nno user has, and they can fail (or pass) for reasons "
        "that do not exist in TD.\n\n"
        "Use a %s interpreter -- a venv built FROM TouchDesigner's own\n"
        "python, so the version matches what ships without inheriting TD's\n"
        "bundled site-packages (that is how a stray --user or bundled\n"
        "package silently flips a verdict):\n"
        "    <TD>/bin/python.exe -m venv dev/.venv-tests\n"
        "    dev/.venv-tests/Scripts/python.exe -m pip install -r dev/embody/unit_tests/requirements-test.txt\n"
        "    dev/.venv-tests/Scripts/python.exe -m pytest\n"
        "\n"
        "NOT dev/.venv -- that is Embody's live Envoy runtime venv: it carries\n"
        "no pytest, and installing one there mutates the running MCP server\n"
        "(field 2026-08-23).\n"
        % (want, have, sys.executable, want))


_require_td_python()


# unit_tests/ -> embody/ -> dev/  (TD's project.folder is the .toe dir)
_DEV_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sandbox_dev_dir() -> str:
    """A throwaway COPY of the files the suite needs, never the real dev/.

    CAGE RULE: ``project.folder`` must never point at the real project.
    The bridge derives config/registry paths from it, and a test that
    early-refuses inside TD (where the runner pid IS a TouchDesigner) can
    fall through off-TD into code that resolves the REAL registry -- the
    2026-07-30 incident: the dev TD exited silently mid-pytest-run with
    no crash record; likely the stale-heartbeat kill (see the runtime cage
    below), but this reach into the live .embody/ cannot be allowed either
    way.
    """
    root = tempfile.mkdtemp(prefix='embody_pytest_sandbox_')
    for rel in ('embody/envoy_bridge.py',
                'embody/Embody/templates/text_envoy_bridge.py'):
        src = os.path.join(_DEV_DIR, rel)
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    return root


class _PytestEmbodyTestCase(unittest.TestCase):
    """Stand-in for TestRunnerExt.EmbodyTestCase off-TD.

    Custom asserts mirror TestRunnerExt exactly; TD-backed fixtures
    raise SkipTest so a test that genuinely needs the live session
    skips instead of erroring.
    """

    maxDiff = None

    # -- custom asserts (mirrored from TestRunnerExt) -----------------

    def assertStartsWith(self, s, prefix, msg=None):
        if not str(s).startswith(prefix):
            raise AssertionError(
                msg or f'{repr(s)} does not start with {repr(prefix)}')

    def assertEndsWith(self, s, suffix, msg=None):
        if not str(s).endswith(suffix):
            raise AssertionError(
                msg or f'{repr(s)} does not end with {repr(suffix)}')

    def assertDictHasKey(self, d, key, msg=None):
        if key not in d:
            raise AssertionError(msg or f'Key {repr(key)} not in dict')

    def assertLen(self, container, expected_len, msg=None):
        actual = len(container)
        if actual != expected_len:
            raise AssertionError(
                msg or f'Expected length {expected_len}, got {actual}')

    # -- TD-backed fixtures: skip cleanly off-TD ----------------------

    @property
    def embody(self):
        raise unittest.SkipTest('requires a live TouchDesigner session')

    @property
    def embody_ext(self):
        raise unittest.SkipTest('requires a live TouchDesigner session')

    @property
    def sandbox(self):
        raise unittest.SkipTest('requires a live TouchDesigner session')


class _SkipOnUse:
    """Any un-shimmed TD access skips the test instead of erroring."""

    def __init__(self, what):
        self._what = what

    def __getattr__(self, name):
        raise unittest.SkipTest(
            f'requires a live TouchDesigner session ({self._what}.{name})')

    def __call__(self, *args, **kwargs):
        raise unittest.SkipTest(
            f'requires a live TouchDesigner session ({self._what}(...))')


def _runner_stub():
    runner_mod = types.SimpleNamespace(EmbodyTestCase=_PytestEmbodyTestCase)
    runner_dat = types.SimpleNamespace(module=runner_mod)

    class _UnitTests(_SkipOnUse):
        def op(self, name):
            if name == 'TestRunnerExt':
                return runner_dat
            raise unittest.SkipTest(
                f'requires a live TouchDesigner session (op({name!r}))')

    class _Op(_SkipOnUse):
        unit_tests = _UnitTests('op.unit_tests')

    return _Op('op')


# --- Runtime cage: the sandbox covers WHICH module is imported, not what it
# reaches at runtime: with no --config, _init_file_logging falls back to
# os.getcwd()/dev/logs, so a repo-root run appended to the LIVE bridge log.
# The TD deaths once blamed on that reach were kill_stale_bridges taskkill'ing
# TD's pid from a stale temp heartbeat the in-TD suite wrote (log 2026-08-29,
# PID 13784; 2026-07-30 likely the same) -- fenced by the repo-root conftest.
_LIVE_BRIDGE_LOG = os.path.join(_DEV_DIR, 'logs', 'envoy-bridge.log')
_live_log_size_at_start = None


def _cage_bridge_runtime():
    """Neutralize file logging in every imported bridge copy.

    Matched by CAPABILITY, not by module name. A name list silently missed
    any copy loaded under a different one: test_envoy_sessions loads the
    bridge via spec_from_file_location as 'envoy_bridge_sessions', so it
    escaped the cage entirely and wrote 19 lines to the live
    dev/logs/envoy-bridge.log -- which is exactly what the sessionfinish
    tripwire below was reporting. Anything exposing _init_file_logging is
    a bridge copy and gets caged, including copies added later.
    """
    targets = [m for m in list(sys.modules.values())
               if m is not None and hasattr(m, '_init_file_logging')]
    # A bridge copy loaded with module_from_spec + exec_module and never
    # registered in sys.modules is invisible to the scan above -- it lives
    # only as an attribute of the test module that built it
    # (test_envoy_bridge_instance_arg.bridge). Those four tests wrote 2070
    # bytes to the live log past BOTH the old name list and a sys.modules
    # capability scan. Reach them through the test modules that hold them.
    seen = {id(m) for m in targets}
    for mod in list(sys.modules.values()):
        if mod is None or not getattr(mod, '__name__', '').startswith('test_'):
            continue
        for attr in list(vars(mod).values()):
            if (isinstance(attr, types.ModuleType)
                    and hasattr(attr, '_init_file_logging')
                    and id(attr) not in seen):
                seen.add(id(attr))
                targets.append(attr)
    for mod in targets:
        handle = getattr(mod, '_log_file', None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        mod._log_file = None
        mod._init_file_logging = lambda *a, **k: None
    return len(targets)


def _require_kill_fence() -> None:
    """Fail closed: --confcutdir can skip the repo-root conftest (the
    process-kill fence) while this file still loads. A run without the
    fence once killed TouchDesigner (2026-09-11)."""
    if getattr(subprocess.Popen.__init__, '_embody_kill_fence', None) is None:
        raise pytest.UsageError(
            'kill fence not installed: the repo-root conftest.py did not '
            'load (--confcutdir/--noconftest?). Run pytest from the repo root.')


def pytest_configure(config):
    _require_kill_fence()  # first: before any sandbox dir is made
    # Inject only when TD is genuinely absent -- inside TD these names
    # are real builtins and must never be shadowed. project.folder points
    # at a SANDBOX COPY (see _sandbox_dev_dir), never the real dev/.
    if not hasattr(builtins, 'project'):
        builtins.project = types.SimpleNamespace(folder=_sandbox_dev_dir())
    if not hasattr(builtins, 'op'):
        builtins.op = _runner_stub()
    global _live_log_size_at_start
    try:
        _live_log_size_at_start = os.path.getsize(_LIVE_BRIDGE_LOG)
    except OSError:
        _live_log_size_at_start = None


def pytest_collection_finish(session):
    """Cage after collection -- test modules import the bridge at import time."""
    _cage_bridge_runtime()


def pytest_sessionfinish(session, exitstatus):
    """Tripwire: THIS process must not have written to the live install.

    Scoped to our own pid, not to file growth. The log is shared -- every live
    AI session's bridge appends to it constantly -- so a size check would fire
    on a concurrent session and get switched off as noise. The bridge tags each
    line "[envoy-bridge:<pid>]", and under pytest the module runs in-process,
    so our own pid appearing in the appended region is proof the cage leaked.
    """
    if _live_log_size_at_start is None:
        return
    try:
        with open(_LIVE_BRIDGE_LOG, 'r', encoding='utf-8', errors='replace') as fh:
            fh.seek(_live_log_size_at_start)
            appended = fh.read()
    except OSError:
        return
    ours = '[envoy-bridge:%d]' % os.getpid()
    mine = [ln for ln in appended.splitlines() if ours in ln]
    if mine:
        session.exitstatus = 1
        print(
            '\nISOLATION FAILURE: this pytest process (pid %d) wrote %d line(s)\n'
            'to the live %s\n'
            'The bridge under test reached the real install instead of the\n'
            'sandbox. Do not trust this run, and do not run it against a live\n'
            'TouchDesigner until the leak is closed. First leaked line:\n  %s'
            % (os.getpid(), len(mine), _LIVE_BRIDGE_LOG, mine[0][:200]))
