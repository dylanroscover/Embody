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
import sys
import tempfile
import types
import unittest


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
        "Use a %s interpreter, e.g. the project venv:\n"
        "    dev/.venv/Scripts/python.exe -m pytest\n"
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
    no crash record; cause unproven, but this class of reach into the
    live .embody/ is exactly what cannot be allowed either way.
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


def pytest_configure(config):
    # Inject only when TD is genuinely absent -- inside TD these names
    # are real builtins and must never be shadowed. project.folder points
    # at a SANDBOX COPY (see _sandbox_dev_dir), never the real dev/.
    if not hasattr(builtins, 'project'):
        builtins.project = types.SimpleNamespace(folder=_sandbox_dev_dir())
    if not hasattr(builtins, 'op'):
        builtins.op = _runner_stub()
