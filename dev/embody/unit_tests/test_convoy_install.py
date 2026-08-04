"""
Test suite: the Convoy host-app installer (Embody/convoy/convoy_install.py).

Dual-runner, like test_convoy_client.py: the module under test is pure
stdlib Python with no TouchDesigner imports, so this whole file runs
under plain pytest on the windows+macos CI matrix AND inside TD via
TestRunnerExt. Nothing here needs a live session.

WHAT CI CAN PROVE, AND WHAT IT CANNOT. Everything below is generation
and decision logic -- the text that gets written, the argv that gets
run, the branch that gets taken. That is genuinely most of the risk,
because the install path is a thing a user pulses ONCE and then never
watches again. What CI cannot prove, and what the docs must therefore
not claim: that an unelevated TD can really register the task, that
supervision really restarts the daemon in ~60 s, that any of it survives
a reboot, or ANY macOS behaviour whatsoever. Those are section 5's
real-machine acceptance items and they stay owed.

TWO CONVENTIONS THIS FILE HOLDS TO:

  - LITERAL EXPECTED PATH STRINGS, never os.path.join. These assertions
    are about a TARGET platform's separators, and os.path.join yields
    the HOST's -- which on the macOS runner turns every backslash
    expectation into a passing test that proves nothing. The same
    mistake failed the first macOS CI run of the bridge suite and has
    bitten this repo three times.

  - NO TEST TOUCHES A REAL SUPERVISOR OR THE REAL DATA DIR. Every
    OS-mutating call goes through an injected runner that records argv
    instead of running it; every path is under a temp dir. A test that
    called the default runner would register an actual Scheduled Task on
    the machine running the suite.
"""

import ast
import builtins
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import venv
import zipfile
from unittest import mock

# unit_tests/ -> embody/ -> dev/ -> the repo root. __file__ is the real
# path under BOTH runners (TestRunnerExt loads these modules with
# spec_from_file_location off disk), so this resolves the checkout even
# though the pytest shim points project.folder at a sandbox copy.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_INSTALL_PATH = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody',
                             'convoy', 'convoy_install.py')
_BRIDGE_PATH = os.path.join(_REPO_ROOT, 'dev', 'embody', 'envoy_bridge.py')
_CONVOY_DIR = os.path.join(_REPO_ROOT, 'dev', 'convoy')
_RUNTIME_BUILDER_PATH = os.path.join(
    _CONVOY_DIR, 'build_convoy_runtime_bundle.py')

_spec = importlib.util.spec_from_file_location('convoy_install',
                                               _INSTALL_PATH)
install_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_mod)
sys.modules[_spec.name] = install_mod

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

WIN_DATA = r'C:\Users\x\AppData\Local\EmbodyConvoy'
MAC_DATA = '/Users/x/Library/Application Support/EmbodyConvoy'
WIN_PY = (r'C:\Program Files\Derivative\TouchDesigner.2025.33070'
          r'\bin\pythonw.exe')
WIN_USER = r'TEC-B4A\admin'
# The win32 account, as the process environment carries it. Injected into
# install() so the Scheduled-Task path resolves an account DETERMINISTICALLY
# on any CI OS: current_user_account reads USERNAME, a Windows variable the
# macos-latest runner does not set, so a win32 install() left to read the
# real os.environ refused with no_user_account and reddened the mac leg of
# bridge-tests. Injecting env (never mutating os.environ -- that has bitten
# this suite before) keeps the real resolver under test with a stable input.
WIN_ENV = {'USERDOMAIN': 'TEC-B4A', 'USERNAME': 'admin'}
MAC_PY = ('/Applications/TouchDesigner.app/Contents/Frameworks/'
          'Python.framework/Versions/Current/bin/python3')

# Guard for the one test that spawns a child interpreter: inside
# TouchDesigner sys.executable may be TouchDesigner.exe, and spawning
# THAT would launch a whole second TD. Only run it when we are certain
# we hold a plain python.
_SPAWNABLE = os.path.basename(sys.executable).lower().startswith('python')


# -- AST helpers -------------------------------------------------------
#
# Several properties below are about what the module DOES, not what it
# SAYS. This module's prose deliberately names the things it refuses to
# do ("never shell=True", "NEVER shutil.rmtree") to explain why, so a
# substring scan cannot tell a warning from a violation -- it goes red on
# the comment and then gets weakened until it proves nothing. Parsing is
# the honest version of the same check.

def _module_ast():
    with open(_INSTALL_PATH, encoding='utf-8') as f:
        return ast.parse(f.read(), _INSTALL_PATH)


def _call_name(node):
    """'os.rmdir' / 'rmtree' for a Call node's target, or ''."""
    func = node.func
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    elif parts:
        parts.append('')
    return '.'.join(reversed(parts))


def _called_names(tree):
    return {_call_name(n) for n in ast.walk(tree)
            if isinstance(n, ast.Call)}


def _imported_modules(tree):
    """Every module named by an import, INCLUDING ones inside functions
    (the launcher template's deferred `import json` is one)."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _free_names(tree):
    """Names LOADED but never bound anywhere in the module, minus the
    builtins -- i.e. what the module actually expects from its globals.

    This is the honest form of "does it touch TouchDesigner": a plain
    name scan flags `run = runner or run_command` as TD's `run()`, and a
    substring scan flags the word inside a comment.
    """
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx,
                                                     (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                bound.add(node.name)
            args = getattr(node, 'args', None)
            if isinstance(args, ast.arguments):
                for group in (args.args, args.posonlyargs, args.kwonlyargs):
                    bound.update(a.arg for a in group)
                for extra in (args.vararg, args.kwarg):
                    if extra is not None:
                        bound.add(extra.arg)
        elif isinstance(node, ast.Import):
            bound.update((a.asname or a.name).split('.')[0]
                         for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            bound.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    loaded = {n.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return loaded - bound - _REAL_BUILTINS


# The TouchDesigner globals this suite refuses to see referenced.
TD_GLOBALS = ('op', 'ops', 'parent', 'me', 'ui', 'project', 'run', 'mod',
              'iop', 'ipar', 'tdu', 'debug', 'opex', 'absTime')

# ...minus them, because BOTH RUNNERS PUT TD GLOBALS IN `builtins`. The
# pytest shim (unit_tests/conftest.py) injects `op` and `project` into
# builtins so this file can import at all, and inside TouchDesigner they
# are genuinely builtins. Subtracting a live dir(builtins) would
# therefore delete exactly the names being searched for, and the check
# would pass forever while proving nothing -- which is what the
# guard-on-the-guard test below caught.
_REAL_BUILTINS = set(dir(builtins)) - set(TD_GLOBALS)


class _Runner:
    """Records argv instead of running it. Every OS-mutating call in the
    module goes through one of these in tests -- the default runner
    really does register a Scheduled Task."""

    def __init__(self, returncode=0, stdout='', stderr=''):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, timeout_s=None):
        self.calls.append(list(argv))
        code = self.returncode
        if callable(code):
            code = code(argv)
        return code, self.stdout, self.stderr


def _launchctl_runner(loaded=False, **kw):
    """A _Runner whose launchctl `print` answers whether the agent label
    is loaded, and whose `bootout` unloads it -- the semantics install()
    now depends on (a loaded label cannot be bootstrapped: EIO 5).
    Everything else succeeds."""
    state = {'loaded': loaded}

    def rc(argv):
        if argv and argv[0] == 'launchctl':
            if argv[1] == 'print':
                return 0 if state['loaded'] else 5
            if argv[1] == 'bootout':
                state['loaded'] = False
        return 0

    return _Runner(returncode=rc, **kw)


def _approved_runtime(data_dir, interpreter, platform=None,
                      architecture=None, runner=None):
    """Bypass only for tests of installer behavior below the runtime gate.

    Dedicated runtime tests exercise the real verifier. Task/plist/order tests
    use this seam because a macOS binary cannot execute on Windows or vice
    versa, and no test may touch a real per-user runtime directory.
    """
    platform = platform or sys.platform
    architecture = ('arm64' if platform == 'darwin' else 'x86_64')
    return {
        'ok': True,
        'runtime_id': 'cpython-3.11-test-%s-%s' % (platform, architecture),
        'platform': platform,
        'architecture': architecture,
        'python_version': '3.11.15',
        'cryptography_version': 'test',
        'archive_sha256': 'a' * 64,
        'interpreter': str(interpreter),
        'receipt_format': install_mod.RUNTIME_RECEIPT_FORMAT,
    }


class _TempDir:
    """tempfile.mkdtemp with a context manager, because the in-TD runner
    and pytest disagree about fixtures."""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix='convoy_install_test_')
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


# -- 1. paths, per platform, as LITERAL strings ------------------------

class TestConvoyInstallPaths(EmbodyTestCase):

    def test_win32_paths_are_backslash_paths(self):
        """LITERAL expectations. os.path.join would answer with forward
        slashes on the macOS runner and assert nothing at all."""
        self.assertEqual(install_mod.install_root(WIN_DATA), WIN_DATA)
        self.assertEqual(
            install_mod.app_dir(WIN_DATA, platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\app')
        self.assertEqual(
            install_mod.app_dir(WIN_DATA, '6.0.171', platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\app\6.0.171')
        self.assertEqual(
            install_mod.bin_dir(WIN_DATA, platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\bin')
        self.assertEqual(
            install_mod.logs_dir(WIN_DATA, platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\logs')
        self.assertEqual(
            install_mod.installed_path(WIN_DATA, platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\installed.json')
        self.assertEqual(
            install_mod.launcher_path(WIN_DATA, platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\bin'
            r'\convoy_host_launch.py')
        self.assertEqual(
            install_mod.log_path(WIN_DATA, platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\logs\host.log')
        self.assertEqual(
            install_mod.complete_path(WIN_DATA, '6.0.171',
                                      platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\app\6.0.171\.complete')
        self.assertEqual(
            install_mod.task_xml_path(WIN_DATA, platform='win32'),
            r'C:\Users\x\AppData\Local\EmbodyConvoy\bin'
            r'\convoy_host_task.xml')

    def test_darwin_paths_are_slash_paths_even_from_windows(self):
        self.assertEqual(
            install_mod.app_dir(MAC_DATA, '6.0.171', platform='darwin'),
            '/Users/x/Library/Application Support/EmbodyConvoy/app/6.0.171')
        self.assertEqual(
            install_mod.launcher_path(MAC_DATA, platform='darwin'),
            '/Users/x/Library/Application Support/EmbodyConvoy/bin/'
            'convoy_host_launch.py')
        self.assertEqual(
            install_mod.log_path(MAC_DATA, platform='darwin'),
            '/Users/x/Library/Application Support/EmbodyConvoy/logs/'
            'host.log')
        self.assertNotIn('\\',
                         install_mod.app_dir(MAC_DATA, '6.0.171',
                                             platform='darwin'))

    def test_the_launch_agent_lives_in_the_users_launchagents_dir(self):
        """launchd loads agents from exactly one directory, and it is
        keyed on HOME, not on the data dir."""
        self.assertEqual(
            install_mod.plist_path('/Users/x'),
            '/Users/x/Library/LaunchAgents/tools.embody.convoy.host.plist')

    def test_the_launcher_path_never_carries_a_version(self):
        """THE upgrade property: the supervisor points here forever, so
        a new version is a file rewrite and never a re-registration."""
        path = install_mod.launcher_path(WIN_DATA, platform='win32')
        self.assertNotIn('6.0', path)
        self.assertIn('bin', path)


class TestConvoyInstallVersionSafety(EmbodyTestCase):
    """The version becomes a DIRECTORY NAME under the user's data dir,
    and remove_payload later unlinks inside it."""

    def test_a_traversing_version_is_refused(self):
        for bad in ('..', '../../bin', r'..\..\bin', '.', '',
                    'a/b', r'a\b', 'looks-safe\n', None, '  '):
            with self.assertRaises(ValueError):
                install_mod.safe_version(bad)

    def test_app_dir_refuses_a_traversing_version(self):
        with self.assertRaises(ValueError):
            install_mod.app_dir(WIN_DATA, '../../bin', platform='win32')

    def test_ordinary_versions_pass(self):
        for good in ('6.0.171', '2025.33070', '6.0.171-rc1', '6.0.171+1'):
            self.assertEqual(install_mod.safe_version(good), good)


# -- 2. the payload: atomicity, .complete last, exact removal ----------

_MODULES = {
    'convoy_hostapp.py': '# hostapp\n',
    'convoy_platform.py': '# platform\n',
    'convoy_hoststore.py': '# hoststore\n',
}


class TestConvoyInstallPayload(EmbodyTestCase):

    def test_every_module_lands_and_complete_is_written(self):
        with _TempDir() as root:
            manifest = install_mod.write_payload(root, '6.0.171', _MODULES)
            target = install_mod.app_dir(root, '6.0.171')
            for name in _MODULES:
                with open(os.path.join(target, name)) as f:
                    self.assertEqual(f.read(), _MODULES[name])
            self.assertEqual(sorted(manifest['files']),
                             sorted(_MODULES))
            self.assertTrue(os.path.isfile(
                os.path.join(target, '.complete')))

    def test_complete_is_written_LAST(self):
        """The interlock the launcher depends on: a payload dir without
        .complete is refused, so a crashed install can never execute."""
        order = []
        real = install_mod._atomic_write

        def spy(path, text):
            order.append(os.path.basename(path))
            return real(path, text)

        with _TempDir() as root:
            install_mod._atomic_write = spy
            try:
                install_mod.write_payload(root, '6.0.171', _MODULES)
            finally:
                install_mod._atomic_write = real
        self.assertEqual(order[-1], '.complete',
                         'the manifest must be the last thing written')
        self.assertEqual(len(order), len(_MODULES) + 1)

    def test_each_file_is_written_through_a_temp_and_replace(self):
        """Atomicity: a reader mid-install sees the old file or the new
        one, never a half-written module."""
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((os.path.basename(src), os.path.basename(dst)))
            return real_replace(src, dst)

        with _TempDir() as root:
            os.replace = spy
            try:
                install_mod.write_payload(root, '6.0.171', _MODULES)
            finally:
                os.replace = real_replace
        self.assertTrue(seen)
        for src, dst in seen:
            self.assertTrue(src.endswith('.tmp'),
                            'writes must go through a temp name')
            self.assertFalse(dst.endswith('.tmp'))

    def test_no_temp_files_are_left_behind(self):
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            target = install_mod.app_dir(root, '6.0.171')
            leftovers = [n for n in os.listdir(target)
                         if n.endswith('.tmp')]
            self.assertEqual(leftovers, [])

    def test_a_rewrite_of_the_same_version_converges(self):
        """Two projects pulsing Install at once must not race into a
        broken payload."""
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            install_mod.write_payload(root, '6.0.171', _MODULES)
            manifest = install_mod.read_manifest(root, '6.0.171')
            self.assertEqual(sorted(manifest['files']), sorted(_MODULES))

    def test_payload_entries_must_be_bare_filenames(self):
        with _TempDir() as root:
            for bad in ('../escape.py', 'sub/dir.py', r'..\escape.py'):
                with self.assertRaises(ValueError):
                    install_mod.write_payload(root, '6.0.171',
                                              {bad: 'x'})

    def test_read_manifest_treats_corrupt_as_absent(self):
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            with open(install_mod.complete_path(root, '6.0.171'), 'w') as f:
                f.write('{not json')
            self.assertIsNone(install_mod.read_manifest(root, '6.0.171'))


class TestConvoyInstallRemovePayload(EmbodyTestCase):

    def test_exactly_the_manifest_entries_are_unlinked(self):
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            outcome = install_mod.remove_payload(root, '6.0.171')
            self.assertEqual(sorted(outcome['removed']),
                             sorted(list(_MODULES) + ['.complete']))
            self.assertTrue(outcome['removed_dir'])
            self.assertFalse(os.path.exists(
                install_mod.app_dir(root, '6.0.171')))

    def test_a_file_we_did_not_install_is_kept_and_the_dir_survives(self):
        """rmdir ONLY, never shutil.rmtree. rmtree would take whatever a
        user had put in that directory; rmdir refuses a non-empty one, so
        the surprise survives and gets reported."""
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            target = install_mod.app_dir(root, '6.0.171')
            stranger = os.path.join(target, 'user_notes.txt')
            with open(stranger, 'w') as f:
                f.write('mine')
            outcome = install_mod.remove_payload(root, '6.0.171')
            self.assertFalse(outcome['removed_dir'])
            self.assertTrue(os.path.isfile(stranger),
                            'an uninstall must never delete what it did '
                            'not install')

    def test_never_calls_rmtree(self):
        """Pinned in code, not just in review: the repo file-safety rule
        forbids shutil.rmtree, and a stuck Filecleanup once turned that
        into 18 deleted specimen files.

        Checked through the AST, not a substring scan -- the module's own
        prose says the words 'shutil.rmtree' precisely to explain why it
        does not call it, and a text search cannot tell a warning from a
        violation."""
        called = _called_names(_module_ast())
        self.assertNotIn('rmtree', called)
        self.assertNotIn('shutil.rmtree', called)
        self.assertNotIn('shutil', _imported_modules(_module_ast()),
                         'importing shutil at all invites rmtree back')
        self.assertIn('os.rmdir', called)

    def test_a_traversing_manifest_entry_is_refused_at_the_use_site(self):
        """The manifest is read back off disk and could have been edited
        between write and use, so the check lives at the unlink, not only
        at the write."""
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            path = install_mod.complete_path(root, '6.0.171')
            with open(path, 'w') as f:
                json.dump({'version': '6.0.171',
                           'files': ['../../../evil.py']}, f)
            outcome = install_mod.remove_payload(root, '6.0.171')
            self.assertIn('../../../evil.py', outcome['kept'])
            self.assertEqual(outcome['removed'], ['.complete'])

    def test_a_DRIVE_RELATIVE_manifest_entry_cannot_escape(self):
        """REGRESSION GUARD, MEASURED 2026-08-01. The deny-list this
        replaced rejected '/', '\\\\', '..' and os.path.isabs -- and
        still admitted 'D:evil.py', which has none of those. On Windows
        ntpath.join(payload_dir, 'D:evil.py') DISCARDS the payload dir
        and resolves against drive D:'s own per-drive current directory,
        so remove_payload unlinked OUTSIDE the payload tree (proven with
        an intercepted os.unlink; it only landed in 'missing' because
        this machine has no D:).

        Enumerating what is dangerous cannot work on Windows paths.
        This is an ACCEPT-list -- the same lesson as safe_version."""
        for escape in ('D:evil.py', 'C:foo', 'D:', 'looks-safe.py\n',
                       'Z:Users/evil.py',
                       '\\\\server\\share\\evil.py', '..\\evil.py',
                       '../evil.py', '/etc/passwd', '.', '..', ''):
            self.assertIsNone(install_mod._bare_name(escape),
                              '%r must never be unlinked' % (escape,))
        for ok in ('convoy_hostapp.py', '.complete', 'a-b_c.1.py'):
            self.assertEqual(install_mod._bare_name(ok), ok)

    def test_nothing_outside_the_payload_dir_is_ever_unlinked(self):
        """The property itself, not just the name check: every path
        remove_payload hands to os.unlink is inside the payload dir."""
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            target = install_mod.app_dir(root, '6.0.171')
            with open(install_mod.complete_path(root, '6.0.171'), 'w') as f:
                json.dump({'version': '6.0.171',
                           'files': ['convoy_hostapp.py', 'D:evil.py',
                                     '..\\..\\evil.py',
                                     'Z:Users/Public/evil.py']}, f)
            attempted = []
            real_unlink = os.unlink

            def spy(path):
                attempted.append(path)
                return real_unlink(path)

            os.unlink = spy
            try:
                outcome = install_mod.remove_payload(root, '6.0.171')
            finally:
                os.unlink = real_unlink
            self.assertTrue(attempted)
            for path in attempted:
                self.assertTrue(path.startswith(target),
                                '%r is outside the payload dir' % (path,))
            for refused in ('D:evil.py', '..\\..\\evil.py',
                            'Z:Users/Public/evil.py'):
                self.assertIn(refused, outcome['kept'])

    def test_our_own_pycache_is_removed_so_the_dir_can_go(self):
        """MEASURED 2026-08-01: running the real daemon makes CPython
        write app/<version>/__pycache__/*.pyc beside the payload, the
        rmdir then fails, and uninstall reported ok=True with kept=[]
        while the whole payload dir survived on every machine that had
        ever actually RUN the host app.

        __pycache__ is a derived artifact of OUR modules inside OUR
        versioned directory, so it is ours to remove -- and the preview
        NAMES it, so this is a stated deletion, not a silent one."""
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            target = install_mod.app_dir(root, '6.0.171')
            cache = os.path.join(target, '__pycache__')
            os.makedirs(cache)
            for name in ('convoy_hostapp.cpython-311.pyc',
                         'convoy_platform.cpython-311.pyc'):
                with open(os.path.join(cache, name), 'wb') as f:
                    f.write(b'\x00')
            outcome = install_mod.remove_payload(root, '6.0.171')
            self.assertTrue(outcome['removed_dir'],
                            'the payload dir must not survive its own '
                            'bytecode cache')
            self.assertFalse(os.path.exists(target))
            self.assertTrue(any('__pycache__' in r
                                for r in outcome['removed']))
            self.assertEqual(outcome['remaining'], [])

    def test_a_surviving_directory_REPORTS_what_is_still_in_it(self):
        """The old version discarded removed_dir entirely and swallowed
        every rmdir, so a payload dir left behind appeared in neither
        'removed', 'kept' nor 'retain' -- it was simply invisible."""
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.171', _MODULES)
            target = install_mod.app_dir(root, '6.0.171')
            stranger = os.path.join(target, 'user_notes.txt')
            with open(stranger, 'w') as f:
                f.write('mine')
            outcome = install_mod.remove_payload(root, '6.0.171')
            self.assertFalse(outcome['removed_dir'])
            self.assertEqual(outcome['remaining'], [stranger])
            self.assertTrue(os.path.isfile(stranger))

    def test_removing_an_absent_payload_is_quiet(self):
        with _TempDir() as root:
            outcome = install_mod.remove_payload(root, '6.0.171')
            self.assertEqual(outcome['removed'], [])
            self.assertFalse(outcome['removed_dir'])

    def test_installed_versions_reads_the_disk_not_the_record(self):
        """An interrupted upgrade leaves a payload installed.json never
        mentioned, and uninstall still has to find it."""
        with _TempDir() as root:
            install_mod.write_payload(root, '6.0.9', _MODULES)
            install_mod.write_payload(root, '6.0.171', _MODULES)
            self.assertEqual(install_mod.installed_versions(root),
                             ['6.0.9', '6.0.171'])


# -- 3. GOLDEN task XML -------------------------------------------------

class TestConvoyInstallTaskXml(EmbodyTestCase):

    def _text(self, user=WIN_USER):
        blob = install_mod.render_task_xml(
            WIN_PY, WIN_DATA + r'\bin\convoy_host_launch.py', user)
        return blob, blob[2:].decode('utf-16-le')

    def test_the_bytes_are_utf16le_with_a_bom(self):
        """schtasks REJECTS the file outright if it is UTF-8, so the
        encoding is asserted in bytes. Explicit LE, not the 'utf-16'
        codec -- that one picks its endianness from the host."""
        blob, text = self._text()
        self.assertEqual(blob[:2], b'\xff\xfe')
        self.assertTrue(text.startswith('<?xml version="1.0" '
                                        'encoding="UTF-16"?>'))
        # Round-trips through the BOM-aware codec too.
        self.assertEqual(blob.decode('utf-16'), text)

    def test_golden_xml(self):
        """The whole document, verbatim. Every line here was settled by
        the 2026-07-31 spike or the registration probe; a diff on this
        test is a deliberate supervision change, not a formatting one."""
        _, text = self._text()
        expected = (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.4" xmlns="http://schemas.microsoft.com/'
            'windows/2004/02/mit/task">\n'
            '  <RegistrationInfo>\n'
            '    <Author>Embody</Author>\n'
            '    <Description>Runs the Embody Convoy host app for this '
            'user and restarts it within a minute if it stops. Per-user '
            'and never elevated.</Description>\n'
            '    <URI>\\EmbodyConvoyHost</URI>\n'
            '  </RegistrationInfo>\n'
            '  <Triggers>\n'
            '    <LogonTrigger>\n'
            '      <Enabled>true</Enabled>\n'
            '      <Repetition>\n'
            '        <Interval>PT1M</Interval>\n'
            '        <StopAtDurationEnd>false</StopAtDurationEnd>\n'
            '      </Repetition>\n'
            '      <UserId>' + WIN_USER + '</UserId>\n'
            '      <Delay>PT30S</Delay>\n'
            '    </LogonTrigger>\n'
            '  </Triggers>\n'
            '  <Principals>\n'
            '    <Principal id="Author">\n'
            '      <UserId>' + WIN_USER + '</UserId>\n'
            '      <LogonType>InteractiveToken</LogonType>\n'
            '      <RunLevel>LeastPrivilege</RunLevel>\n'
            '    </Principal>\n'
            '  </Principals>\n'
            '  <Settings>\n'
            '    <MultipleInstancesPolicy>IgnoreNew'
            '</MultipleInstancesPolicy>\n'
            '    <DisallowStartIfOnBatteries>false'
            '</DisallowStartIfOnBatteries>\n'
            '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
            '    <AllowHardTerminate>true</AllowHardTerminate>\n'
            '    <StartWhenAvailable>true</StartWhenAvailable>\n'
            '    <RunOnlyIfNetworkAvailable>false'
            '</RunOnlyIfNetworkAvailable>\n'
            '    <IdleSettings>\n'
            '      <StopOnIdleEnd>false</StopOnIdleEnd>\n'
            '      <RestartOnIdle>false</RestartOnIdle>\n'
            '    </IdleSettings>\n'
            '    <AllowStartOnDemand>true</AllowStartOnDemand>\n'
            '    <Enabled>true</Enabled>\n'
            '    <Hidden>true</Hidden>\n'
            '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n'
            '    <WakeToRun>false</WakeToRun>\n'
            '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
            '    <Priority>7</Priority>\n'
            '  </Settings>\n'
            '  <Actions Context="Author">\n'
            '    <Exec>\n'
            '      <Command>' + WIN_PY + '</Command>\n'
            '      <Arguments>"' + WIN_DATA +
            '\\bin\\convoy_host_launch.py"</Arguments>\n'
            '    </Exec>\n'
            '  </Actions>\n'
            '</Task>\n')
        self.assertEqual(text, expected)

    def test_NO_restart_on_failure_elements_exist(self):
        """THE NEGATIVE ASSERTION, and the spike's lesson pinned in code.
        RestartOnFailure/RestartCount/RestartInterval respond to a
        failure to LAUNCH the task, NOT to a child process that died --
        so they do nothing here, and adding them back would look like
        supervision while providing none. The repetition trigger is the
        supervisor."""
        _, text = self._text()
        for element in ('RestartOnFailure', 'RestartCount',
                        'RestartInterval'):
            self.assertNotIn(element, text,
                             '%s does nothing for a dying CHILD; the '
                             'PT1M repetition is the supervisor' % element)

    def test_the_settings_that_quietly_kill_a_daemon_are_pinned(self):
        """None of these can be caught by an acceptance run short enough
        to sit through: P3D takes three days, the battery flags need an
        unplug, StopOnIdleEnd needs an idle transition."""
        _, text = self._text()
        self.assertIn('<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>', text)
        self.assertNotIn('P3D', text)
        self.assertIn('<DisallowStartIfOnBatteries>false'
                      '</DisallowStartIfOnBatteries>', text)
        self.assertIn('<StopIfGoingOnBatteries>false'
                      '</StopIfGoingOnBatteries>', text)
        self.assertIn('<StopOnIdleEnd>false</StopOnIdleEnd>', text)

    def test_the_LOGON_TRIGGER_MUST_CARRY_A_USERID(self):
        """REGRESSION GUARD FOR A MEASURED, SHIPPED-BLOCKING DEFECT.
        DO NOT 'SIMPLIFY' THE UserId AWAY.

        A LogonTrigger with no UserId means 'when ANY user logs on',
        which only an administrator may register. This installer runs
        inside a NON-ELEVATED TouchDesigner, so without it schtasks
        answers `ERROR: Access is denied.` and the install fails at its
        final step -- on every machine, every time.

        MEASURED 2026-08-01 against real schtasks, one process, one
        moment, three XMLs differing ONLY here:
            (A) no UserId anywhere              -> Access is denied
            (B) UserId in the LogonTrigger only -> SUCCESS
            (C) UserId in trigger AND principal -> SUCCESS
        We emit (C).

        No other test in this file can catch it: the UserId-less
        document is perfectly well-formed, round-trips through
        ElementTree, and satisfies every other assertion here. That is
        exactly why this one asserts the rendered text directly.

        AND WHY IT MUST KEEP DOING SO. Re-verified 2026-08-01 by
        registering this output for real and exporting it back with
        `schtasks /Query /XML`: WINDOWS NORMALIZES THE TWO UserId VALUES
        DIFFERENTLY FROM EACH OTHER ON STORAGE --
            LogonTrigger UserId -> S-1-5-21-...-1000  (a SID)
            Principal    UserId -> TEC-A4D\\admin     (the account name)
        Same account, spelled two ways. So do NOT "fix" this test by
        comparing against stored output: an assertion that the stored
        trigger UserId equals the account we submitted WILL FAIL against
        a task that is working perfectly. Same discipline as RunLevel,
        which Windows drops entirely for an unelevated registration.
        """
        _, text = self._text()
        self.assertIn('<UserId>%s</UserId>' % WIN_USER, text)
        trigger = text.split('<Triggers>')[1].split('</Triggers>')[0]
        self.assertIn('<UserId>%s</UserId>' % WIN_USER, trigger,
                      'a LogonTrigger without a UserId is an '
                      'administrator-only registration')
        principal = text.split('<Principals>')[1].split('</Principals>')[0]
        self.assertIn('<UserId>%s</UserId>' % WIN_USER, principal)

    def test_the_trigger_and_the_principal_name_the_SAME_account(self):
        """The trigger says whose logon starts it; the principal says
        who it runs as. Letting the two be filled in separately is how
        they drift into starting for one user and running as another.

        TRUE OF THE SUBMITTED DOCUMENT ONLY -- measured 2026-08-01,
        Windows stores the trigger's UserId as a SID and the principal's
        as the account name, so these two strings are deliberately NOT
        equal once registered. Assert the rendering, never the readback.
        """
        _, text = self._text()
        found = re.findall(r'<UserId>([^<]*)</UserId>', text)
        self.assertEqual(len(found), 2,
                         'exactly two UserId elements: trigger + principal')
        self.assertEqual(found[0], found[1])

    def test_rendering_without_an_account_is_REFUSED_not_defaulted(self):
        """Silently resolving the account is how the UserId went missing
        the first time -- the document stayed valid and every test
        passed. A caller that cannot name the user must fail loudly."""
        for missing in (None, '', '   '):
            with self.assertRaises(ValueError):
                install_mod.render_task_xml(WIN_PY, 'C:\\l.py', missing)

    def test_an_account_with_xml_special_characters_is_escaped(self):
        blob = install_mod.render_task_xml(WIN_PY, 'C:\\l.py',
                                           'DOM&AIN\\a<b>')
        text = blob[2:].decode('utf-16-le')
        self.assertIn('<UserId>DOM&amp;AIN\\a&lt;b&gt;</UserId>', text)
        from xml.etree import ElementTree
        ElementTree.fromstring(text)

    def test_the_supervision_mechanism_is_present(self):
        _, text = self._text()
        self.assertIn('<Interval>PT1M</Interval>', text)
        self.assertIn('<MultipleInstancesPolicy>IgnoreNew'
                      '</MultipleInstancesPolicy>', text)

    def test_runlevel_is_submitted_least_privilege_never_highest(self):
        """ASYMMETRY, VERIFIED 2026-07-31, DO NOT 'FIX' THIS TEST:
        RunLevel is what we SUBMIT. Windows does NOT store it back --
        exporting the registered task with `schtasks /Query /XML` shows
        RunLevel ABSENT for an unelevated registration. So this asserts
        the RENDERED xml; any test comparing against what Windows STORES
        must expect it to be missing, or it will fail against reality.

        ONE OF TWO such asymmetries -- the other is the UserId pair,
        which Windows stores as a SID in the trigger and an account name
        in the principal. See render_task_xml's SUBMITTED IS NOT STORED
        note for the full measured list."""
        _, text = self._text()
        self.assertIn('<RunLevel>LeastPrivilege</RunLevel>', text)
        self.assertNotIn('HighestAvailable', text)

    def test_it_is_well_formed_xml(self):
        from xml.etree import ElementTree
        _, text = self._text()
        ElementTree.fromstring(text)

    def test_paths_with_xml_special_characters_are_escaped(self):
        blob = install_mod.render_task_xml(
            r'C:\P & D\python.exe', r'C:\a<b>\launch.py', WIN_USER)
        text = blob[2:].decode('utf-16-le')
        self.assertIn('C:\\P &amp; D\\python.exe', text)
        self.assertIn('&lt;b&gt;', text)
        from xml.etree import ElementTree
        ElementTree.fromstring(text)


class TestConvoyInstallAccountResolution(EmbodyTestCase):
    """env injected (D-5), so the domain-joined and workgroup shapes are
    both exercised on any machine."""

    def test_a_domain_joined_account(self):
        self.assertEqual(
            install_mod.current_user_account(
                'win32', {'USERDOMAIN': 'TEC-B4A', 'USERNAME': 'admin'}),
            r'TEC-B4A\admin')

    def test_a_workgroup_machine_with_no_domain(self):
        self.assertEqual(
            install_mod.current_user_account('win32',
                                             {'USERNAME': 'admin'}),
            'admin')

    def test_no_username_yields_none_rather_than_a_guess(self):
        """A wrong account registers a task that then runs for somebody
        else -- far worse than a refusal the installer can report."""
        for env in ({}, {'USERDOMAIN': 'TEC-B4A'}, {'USERNAME': '   '}):
            self.assertIsNone(
                install_mod.current_user_account('win32', env))

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(
            install_mod.current_user_account(
                'win32', {'USERDOMAIN': ' TEC-B4A ',
                          'USERNAME': ' admin '}),
            r'TEC-B4A\admin')


class TestConvoyInstallAccountContextAudit(EmbodyTestCase):
    """Swept after the 2026-08-01 UserId defect: every artifact that is
    scoped to an account either NAMES it or is per-user by construction,
    and the difference is asserted, not left to omission."""

    def test_the_launch_agent_is_per_user_by_construction(self):
        """A LaunchAgent needs no account key three times over: it lives
        in ~/Library/LaunchAgents, it is bootstrapped into gui/<uid>,
        and launchd runs an agent as that domain's owner. UserName /
        GroupName are the LaunchDAEMON form -- they need root and would
        be a far larger grant than the install dialog asks for."""
        parsed = plistlib.loads(install_mod.render_launch_agent_plist(
            MAC_PY, '/l.py', MAC_DATA).encode('utf-8'))
        for account_key in ('UserName', 'GroupName', 'InitGroups',
                            'SessionCreate'):
            self.assertNotIn(account_key, parsed)
        self.assertEqual(
            install_mod.plist_path('/Users/x').split('/')[:4],
            ['', 'Users', 'x', 'Library'])

    def test_schtasks_is_never_handed_a_credential(self):
        """/RU and /RP would run the task as another account and would
        mean Embody asking for, holding and passing a password. WHO the
        task belongs to is settled once, in the registered XML."""
        for action in ('register', 'unregister', 'enable', 'disable',
                       'start', 'stop', 'status'):
            argv = install_mod.supervisor_argv(action, 'win32',
                                               xml_path='x.xml')
            # Exact TOKEN comparison, not substring: '/RU' is a
            # substring of the perfectly legitimate '/Run'.
            tokens = {a.upper() for a in argv}
            for credential_flag in ('/RU', '/RP', '/U', '/P'):
                self.assertNotIn(credential_flag, tokens)

    def test_launchctl_never_targets_the_system_domain(self):
        """system/ is root and all users -- a different grant entirely.
        Every target is gui/<uid>, which IS the account."""
        for action in ('register', 'unregister', 'enable', 'disable',
                       'start', 'stop', 'status'):
            argv = install_mod.supervisor_argv(action, 'darwin',
                                               plist_path='a.plist',
                                               uid=501)
            joined = ' '.join(argv)
            self.assertNotIn('system/', joined)
            self.assertIn('gui/501', joined)


# -- 4. GOLDEN plist ----------------------------------------------------

class TestConvoyInstallLaunchAgentPlist(EmbodyTestCase):

    def _text(self):
        return install_mod.render_launch_agent_plist(
            MAC_PY, MAC_DATA + '/bin/convoy_host_launch.py', MAC_DATA)

    def test_golden_plist(self):
        expected = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            '\t<key>Label</key>\n'
            '\t<string>tools.embody.convoy.host</string>\n'
            '\t<key>ProgramArguments</key>\n'
            '\t<array>\n'
            '\t\t<string>' + MAC_PY + '</string>\n'
            '\t\t<string>' + MAC_DATA + '/bin/convoy_host_launch.py'
            '</string>\n'
            '\t</array>\n'
            '\t<key>RunAtLoad</key>\n'
            '\t<true/>\n'
            '\t<key>KeepAlive</key>\n'
            '\t<true/>\n'
            '\t<key>ThrottleInterval</key>\n'
            '\t<integer>10</integer>\n'
            '\t<key>ProcessType</key>\n'
            '\t<string>Interactive</string>\n'
            '\t<key>StandardErrorPath</key>\n'
            '\t<string>' + MAC_DATA + '/logs/launchd-stderr.log</string>\n'
            '</dict>\n'
            '</plist>\n')
        self.assertEqual(self._text(), expected)

    def test_macos_really_parses_it(self):
        """plistlib is the same parser launchd's format is defined by --
        a plist that does not parse would fail at bootstrap with a
        message no user could act on."""
        parsed = plistlib.loads(self._text().encode('utf-8'))
        self.assertEqual(parsed['Label'], 'tools.embody.convoy.host')
        self.assertEqual(parsed['ProgramArguments'],
                         [MAC_PY,
                          MAC_DATA + '/bin/convoy_host_launch.py'])
        self.assertIs(parsed['RunAtLoad'], True)
        self.assertIs(parsed['KeepAlive'], True)
        self.assertEqual(parsed['ThrottleInterval'], 10)
        self.assertEqual(parsed['ProcessType'], 'Interactive')

    def test_keepalive_is_the_macos_supervisor(self):
        """launchd genuinely watches the child, which Task Scheduler does
        not -- ~1 s self-heal against Windows' up-to-60. The docs must
        STATE that asymmetry rather than paper over it."""
        parsed = plistlib.loads(self._text().encode('utf-8'))
        self.assertIs(parsed['KeepAlive'], True)

    def test_launchd_does_not_share_the_launchers_log_file(self):
        """The launcher rebinds Python's streams onto host.log; letting
        launchd append to the same path from an independent file offset
        would interleave two writers into one file."""
        parsed = plistlib.loads(self._text().encode('utf-8'))
        self.assertNotIn('StandardOutPath', parsed)
        self.assertNotIn('host.log', parsed['StandardErrorPath'])


# -- 5. GOLDEN launcher, and the pythonw trap --------------------------

class TestConvoyInstallLauncherText(EmbodyTestCase):

    def test_it_is_valid_python_on_both_platforms(self):
        for platform, data in (('win32', WIN_DATA), ('darwin', MAC_DATA)):
            text = install_mod.render_launcher(platform, data)
            compile(text, 'convoy_host_launch.py', 'exec')

    def test_a_windows_data_dir_is_embedded_as_a_safe_literal(self):
        """A Windows path inside a plain "..." would turn \\b, \\t and
        \\n in a user name into control characters -- 'C:\\Users\\bob'
        is a backspace away from a launcher that reads the wrong dir."""
        text = install_mod.render_launcher(
            'win32', r'C:\Users\bob\AppData\Local\EmbodyConvoy')
        namespace = {}
        exec(compile(text.split('def _open_log')[0], 'x', 'exec'),
             namespace)
        self.assertEqual(namespace['DATA_DIR'],
                         r'C:\Users\bob\AppData\Local\EmbodyConvoy')

    def test_the_stream_rebind_PRECEDES_importing_or_calling_main(self):
        """THE pythonw TRAP. Under pythonw.exe sys.stderr is None and
        convoy_hostapp.main() writes to it unconditionally at startup --
        an AttributeError on every launch, i.e. a silent 60-second death
        loop that looks EXACTLY like healthy supervision. The rebind is
        not logging polish; it is what lets the daemon start.

        Asserted STRUCTURALLY, through the AST. The first version of
        this test used text.index('import convoy_hostapp'), which
        silently started matching a COMMENT that mentions the phrase --
        it was measuring prose, not code, and would have gone on
        "passing" against a launcher that imported first.
        """
        text = install_mod.render_launcher('win32', WIN_DATA)
        tree = ast.parse(text)
        functions = {n.name: n for n in tree.body
                     if isinstance(n, ast.FunctionDef)}

        # _open_log is what rebinds the streams.
        targets = set()
        for node in ast.walk(functions['_open_log']):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute):
                        targets.add(t.attr)
        self.assertTrue({'stdout', 'stderr'} <= targets,
                        '_open_log must rebind stdout and stderr')

        # ...and main() calls it BEFORE the daemon is imported or run.
        # Compared by SOURCE LINE, not by ast.walk order -- walk is
        # breadth-first and its indices say nothing about which
        # statement runs first.
        opened = imported = called = None
        for node in ast.walk(functions['main']):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name == '_open_log' and opened is None:
                    opened = node.lineno
                elif name == 'convoy_hostapp.main' and called is None:
                    called = node.lineno
            elif isinstance(node, ast.Import) and imported is None:
                if any(a.name == 'convoy_hostapp' for a in node.names):
                    imported = node.lineno
        self.assertIsNotNone(opened, 'main() never calls _open_log()')
        self.assertIsNotNone(imported)
        self.assertIsNotNone(called)
        self.assertLess(opened, imported,
                        'the daemon is imported before the streams are '
                        'rebound -- the pythonw death loop is back')
        self.assertLess(opened, called)

    def test_it_runs_the_daemon_in_process_and_never_spawns(self):
        """IgnoreNew suppresses the per-minute relaunch only while the
        task INSTANCE lives, and it lives exactly as long as this
        process. A launcher that spawned a child and exited would report
        'finished' at once and the next repetition would start a SECOND
        daemon -- two on one data dir, which is the sweep hazard the
        singleton lock exists for."""
        text = install_mod.render_launcher('win32', WIN_DATA)
        self.assertIn('import convoy_hostapp', text)
        self.assertIn('convoy_hostapp.main(', text)
        for spawner in ('subprocess', 'Popen', 'os.spawn', 'os.execv',
                        'multiprocessing'):
            self.assertNotIn(spawner, text)

    def test_it_refuses_a_payload_without_a_complete_manifest(self):
        text = install_mod.render_launcher('win32', WIN_DATA)
        self.assertIn(".complete", text)
        self.assertIn('refusing to run it', text)

    def test_it_caps_the_log(self):
        text = install_mod.render_launcher('win32', WIN_DATA)
        self.assertIn('LOG_MAX_BYTES = %s' % install_mod.LOG_MAX_BYTES,
                      text)
        self.assertIn('log restarted', text)

    def test_it_passes_the_drain_interval_so_dispatch_is_autonomous(self):
        """--drain-interval defaults to 0 (OFF) in the daemon: a
        supervised host app that never drained its own queue would relay
        nothing at all."""
        text = install_mod.render_launcher('win32', WIN_DATA)
        self.assertIn('"--drain-interval"', text)
        self.assertIn('"--data-dir", DATA_DIR', text)


class TestConvoyInstallLauncherReallyRuns(EmbodyTestCase):
    """Execute the generated launcher for real, with sys.stdout and
    sys.stderr forced to None -- reproducing pythonw's condition on ANY
    platform, without needing pythonw.exe or a Mac."""

    def _run(self, root, expect_rc=None):
        launcher = install_mod.launcher_path(root)
        code = ('import sys, runpy; sys.stdout = None; sys.stderr = None; '
                'runpy.run_path(%r, run_name="__main__")' % (launcher,))
        env = dict(os.environ)
        runtime_root = install_mod.runtime_dir(root, 'test-runtime')
        prior = env.get('PYTHONPATH')
        env['PYTHONPATH'] = (runtime_root + (os.pathsep + prior
                                             if prior else ''))
        record = install_mod.read_installed(root)
        interpreter = ((record or {}).get('interpreter') or sys.executable)
        proc = subprocess.run([interpreter, '-c', code],
                              capture_output=True, text=True, timeout=120,
                              env=env)
        if expect_rc is not None:
            self.assertEqual(proc.returncode, expect_rc,
                             'stderr: %s' % (proc.stderr,))
        return proc

    def _log(self, root):
        try:
            with open(install_mod.log_path(root), encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    def _install_stub(self, root, version='6.0.171', complete=True,
                      record=True):
        """A payload whose convoy_hostapp records what main() saw."""
        stub = (
            'import json, os, sys\n'
            'def main(argv=None):\n'
            '    # THE ASSERTION: under pythonw these are None unless\n'
            '    # the launcher rebound them BEFORE importing us.\n'
            '    marker = {"stderr_is_none": sys.stderr is None,\n'
            '              "stdout_is_none": sys.stdout is None,\n'
            '              "argv": list(argv or [])}\n'
            '    sys.stderr.write("stub daemon reached\\n")\n'
            '    with open(os.path.join(%r, "marker.json"), "w") as f:\n'
            '        json.dump(marker, f)\n'
            '    return 0\n' % (root,))
        modules = dict(_MODULES)
        modules['convoy_hostapp.py'] = stub
        modules['convoy_hostkeys.py'] = (
            'def cryptography_available():\n'
            '    return True\n')
        install_mod.write_payload(root, version, modules)
        if not complete:
            os.unlink(install_mod.complete_path(root, version))
        if record:
            runtime_root = install_mod.runtime_dir(root, 'test-runtime')
            venv.EnvBuilder(with_pip=False).create(runtime_root)
            if sys.platform == 'win32':
                python_rel = 'Scripts/python.exe'
            else:
                python_rel = 'bin/python'
            interpreter = os.path.join(runtime_root,
                                       *python_rel.split('/'))
            with open(os.path.join(runtime_root, 'cryptography.py'),
                      'w') as f:
                f.write('__version__ = "test"\n')
            with open(interpreter, 'rb') as f:
                python_bytes = f.read()
            receipt = {
                'format': install_mod.RUNTIME_RECEIPT_FORMAT,
                'runtime_id': 'test-runtime',
                'python': python_rel,
                'cryptography_version': 'test',
                'archive_sha256': 'a' * 64,
                'files': [{
                    'path': python_rel,
                    'size': len(python_bytes),
                    'sha256': hashlib.sha256(python_bytes).hexdigest(),
                    'mode': 0o755,
                }],
            }
            with open(os.path.join(runtime_root, install_mod.COMPLETE_FILE),
                      'w', encoding='utf-8') as f:
                json.dump(receipt, f)
            install_mod.write_installed(root, {
                'version': version,
                'drain_interval': 2.0,
                'interpreter': interpreter,
                'runtime': {
                    'format': install_mod.RUNTIME_RECEIPT_FORMAT,
                    'runtime_id': 'test-runtime',
                    'cryptography_version': 'test',
                    'archive_sha256': 'a' * 64,
                },
            })
        install_mod._atomic_write(install_mod.launcher_path(root),
                                  install_mod.render_launcher(
                                      sys.platform, root))

    @unittest.skipUnless(_SPAWNABLE,
                         'sys.executable is not a plain python (inside TD '
                         'it may be TouchDesigner.exe)')
    def test_main_is_reached_with_streams_already_rebound(self):
        """THE test the whole launcher exists to pass. If the rebind
        regressed, sys.stderr would still be None here and the daemon
        would die before writing a single line -- while the Scheduled
        Task went on reporting success once a minute forever."""
        with _TempDir() as root:
            self._install_stub(root)
            self._run(root, expect_rc=0)
            with open(os.path.join(root, 'marker.json')) as f:
                marker = json.load(f)
            self.assertFalse(marker['stderr_is_none'],
                             'sys.stderr was still None at main() -- the '
                             'pythonw death loop is back')
            self.assertFalse(marker['stdout_is_none'])
            self.assertIn('--data-dir', marker['argv'])
            self.assertIn('--drain-interval', marker['argv'])
            self.assertIn(root, marker['argv'])

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_everything_written_to_the_daemons_stderr_lands_in_the_log(self):
        with _TempDir() as root:
            self._install_stub(root)
            self._run(root, expect_rc=0)
            log = self._log(root)
            self.assertIn('stub daemon reached', log)
            self.assertIn('starting Convoy host app 6.0.171', log)

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_a_payload_without_complete_is_refused_and_says_so(self):
        with _TempDir() as root:
            self._install_stub(root, complete=False)
            self._run(root, expect_rc=1)
            self.assertIn('incomplete', self._log(root))
            self.assertFalse(os.path.exists(
                os.path.join(root, 'marker.json')),
                'an incomplete payload must never be imported')

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_legacy_install_without_managed_runtime_receipt_fails_closed(self):
        with _TempDir() as root:
            self._install_stub(root)
            install_mod.write_installed(root, {
                'version': '6.0.171',
                'drain_interval': 2.0,
                'interpreter': sys.executable,
            })
            self._run(root, expect_rc=1)
            self.assertIn('no verified managed-runtime receipt',
                          self._log(root))

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_changed_runtime_receipt_fails_closed_before_daemon_import(self):
        with _TempDir() as root:
            self._install_stub(root)
            receipt = os.path.join(install_mod.runtime_dir(
                root, 'test-runtime'), install_mod.COMPLETE_FILE)
            with open(receipt, 'w', encoding='utf-8') as f:
                f.write('{not-json')
            self._run(root, expect_rc=1)
            self.assertIn('receipt is absent or changed', self._log(root))
            self.assertFalse(os.path.exists(os.path.join(root, 'marker.json')))

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_no_install_record_exits_one_and_says_so(self):
        """Exit 1, NOT 0: nothing to run is a real fault and
        LastTaskResult should read as one. (The singleton no-op is the
        opposite case and exits 0 -- see the daemon's --singleton.)"""
        with _TempDir() as root:
            self._install_stub(root, record=False)
            self.assertFalse(os.path.exists(
                install_mod.installed_path(root)))
            self._run(root, expect_rc=1)
            self.assertIn('no Convoy host app is installed',
                          self._log(root))

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_a_TRAVERSING_VERSION_is_refused_before_anything_is_imported(
            self):
        """REGRESSION GUARD, DEMONSTRATED BY EXECUTION 2026-08-01.

        The launcher was the one consumer that skipped safe_version.
        With installed.json = {"version": "../../elsewhere"} it escaped
        the app/ tree entirely, found a .complete out there, ran
        sys.path.insert + `import convoy_hostapp`, executed that file's
        module-level code and exited 0 -- at EVERY LOGIN.

        Inside the stated trust model that is not an escalation (anyone
        who can write the data dir already owns the user), but it turns
        a single file write into a login-persistence primitive in the
        one component that runs whether or not TouchDesigner is open,
        and it defeated the module's own guard.

        Proven the same way as the stream rebind: by running it."""
        with _TempDir() as root:
            self._install_stub(root)
            # Plant a complete, importable payload OUTSIDE app/ and aim
            # the record at it the way the escape did.
            elsewhere = os.path.join(root, 'elsewhere')
            os.makedirs(elsewhere, exist_ok=True)
            with open(os.path.join(elsewhere, 'convoy_hostapp.py'),
                      'w') as f:
                f.write('import os\n'
                        'open(os.path.join(%r, "ESCAPED"), "w").close()\n'
                        'def main(argv=None):\n    return 0\n' % (root,))
            with open(os.path.join(elsewhere, '.complete'), 'w') as f:
                json.dump({'version': 'x', 'files': ['convoy_hostapp.py']},
                          f)
            for version in ('.', '..', '../elsewhere', '..\\elsewhere',
                            '../../elsewhere', 'a/../../elsewhere'):
                install_mod.write_installed(root, {'version': version,
                                                   'drain_interval': 2.0})
                self._run(root, expect_rc=1)
                self.assertFalse(
                    os.path.exists(os.path.join(root, 'ESCAPED')),
                    '%r escaped app/ and its module-level code RAN'
                    % (version,))
            self.assertIn('refusing an unusable version', self._log(root))

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_dot_runtime_id_is_refused_before_receipt_path_resolution(self):
        with _TempDir() as root:
            self._install_stub(root)
            record = install_mod.read_installed(root)
            record['runtime']['runtime_id'] = '..'
            install_mod.write_installed(root, record)
            self._run(root, expect_rc=1)
            self.assertIn('unusable managed runtime', self._log(root))
            self.assertFalse(os.path.exists(os.path.join(root, 'marker.json')))

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_an_oversized_log_is_truncated_and_restarted(self):
        with _TempDir() as root:
            self._install_stub(root)
            os.makedirs(install_mod.logs_dir(root), exist_ok=True)
            with open(install_mod.log_path(root), 'w') as f:
                f.write('x' * (install_mod.LOG_MAX_BYTES + 1024))
            self._run(root, expect_rc=0)
            log = self._log(root)
            self.assertLess(len(log), install_mod.LOG_MAX_BYTES)
            self.assertIn('log restarted', log)
            self.assertNotIn('x' * 100, log)

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_one_long_running_process_cannot_grow_the_log_past_the_cap(self):
        """The supervisor does not restart a healthy host app, so checking
        the file only when the launcher starts is not a lifetime bound."""
        with _TempDir() as root:
            version = '6.0.171'
            self._install_stub(root, version=version)
            stub_path = os.path.join(
                install_mod.app_dir(root, version), 'convoy_hostapp.py')
            with open(stub_path, 'w', encoding='utf-8') as f:
                f.write(
                    'import sys\n'
                    'def main(argv=None):\n'
                    '    sys.stderr.write("z" * %d)\n'
                    '    sys.stderr.flush()\n'
                    '    return 0\n'
                    % (install_mod.LOG_MAX_BYTES + 1024,))
            self._run(root, expect_rc=0)
            path = install_mod.log_path(root)
            self.assertLessEqual(os.path.getsize(path),
                                 install_mod.LOG_MAX_BYTES)
            self.assertIn('log restarted', self._log(root))

    @unittest.skipUnless(_SPAWNABLE, 'needs a plain python')
    def test_an_ordinary_log_is_appended_not_clobbered(self):
        with _TempDir() as root:
            self._install_stub(root)
            os.makedirs(install_mod.logs_dir(root), exist_ok=True)
            with open(install_mod.log_path(root), 'w') as f:
                f.write('earlier launch\n')
            self._run(root, expect_rc=0)
            self.assertIn('earlier launch', self._log(root))


# -- 6. supervisor argv -------------------------------------------------

class TestConvoyInstallSupervisorArgv(EmbodyTestCase):

    ACTIONS = ('register', 'unregister', 'enable', 'disable', 'start',
               'stop', 'status')

    def test_every_action_returns_a_list_of_strings(self):
        """LIST ARGS. The interpreter, launcher and data dir all sit
        under paths with spaces, and a shell string would be one quoting
        bug away from running an attacker-chosen command."""
        for action in self.ACTIONS:
            for platform, extra in (('win32', {'xml_path': 'x.xml'}),
                                    ('darwin', {'plist_path': 'a.plist',
                                                'uid': 501})):
                argv = install_mod.supervisor_argv(action, platform,
                                                   **extra)
                self.assertIsInstance(argv, list)
                for token in argv:
                    self.assertIsInstance(
                        token, str,
                        '%s/%s produced a non-string arg' % (platform,
                                                             action))

    def test_no_subprocess_call_ever_sets_shell_true(self):
        """Through the AST, not a substring scan: the module's prose
        says 'never shell=True' to explain the rule, and a text search
        cannot tell the explanation from a violation.

        A shell would re-parse paths that routinely contain spaces
        ("C:\\Program Files\\...") and turn every one of these calls into
        a quoting bug away from running an attacker-chosen command."""
        tree = _module_ast()
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in ('subprocess.run', 'subprocess.Popen',
                            'subprocess.call', 'subprocess.check_output',
                            'os.system', 'os.popen'):
                continue
            self.assertNotIn(name, ('os.system', 'os.popen'),
                             '%s is a shell by definition' % name)
            checked += 1
            for keyword in node.keywords:
                if keyword.arg == 'shell':
                    self.fail('shell= passed to %s' % name)
            # ...and the first argument is a LIST, not a string.
            self.assertTrue(node.args, '%s got no argv' % name)
            first = node.args[0]
            self.assertFalse(isinstance(first, ast.Constant)
                             and isinstance(first.value, str),
                             '%s was handed a string command line' % name)
        self.assertEqual(checked, 1,
                         'exactly one subprocess call is expected here')

    def test_win32_argv(self):
        self.assertEqual(
            install_mod.supervisor_argv('register', 'win32',
                                        xml_path=r'C:\t.xml'),
            ['schtasks', '/Create', '/TN', 'EmbodyConvoyHost',
             '/XML', r'C:\t.xml', '/F'])
        self.assertEqual(
            install_mod.supervisor_argv('unregister', 'win32'),
            ['schtasks', '/Delete', '/TN', 'EmbodyConvoyHost', '/F'])
        self.assertEqual(
            install_mod.supervisor_argv('disable', 'win32'),
            ['schtasks', '/Change', '/TN', 'EmbodyConvoyHost', '/DISABLE'])
        self.assertEqual(
            install_mod.supervisor_argv('enable', 'win32'),
            ['schtasks', '/Change', '/TN', 'EmbodyConvoyHost', '/ENABLE'])
        self.assertEqual(
            install_mod.supervisor_argv('start', 'win32'),
            ['schtasks', '/Run', '/TN', 'EmbodyConvoyHost'])
        self.assertEqual(
            install_mod.supervisor_argv('stop', 'win32'),
            ['schtasks', '/End', '/TN', 'EmbodyConvoyHost'])
        self.assertEqual(
            install_mod.supervisor_argv('status', 'win32'),
            ['schtasks', '/Query', '/TN', 'EmbodyConvoyHost',
             '/FO', 'LIST', '/V'])

    def test_darwin_argv_is_scoped_to_the_users_gui_domain(self):
        """gui/<uid>, never system/: this is a per-USER agent, and a
        system domain would need root and would be a different grant."""
        self.assertEqual(
            install_mod.supervisor_argv('register', 'darwin',
                                        plist_path='/p/a.plist', uid=501),
            ['launchctl', 'bootstrap', 'gui/501', '/p/a.plist'])
        self.assertEqual(
            install_mod.supervisor_argv('unregister', 'darwin', uid=501),
            ['launchctl', 'bootout', 'gui/501/tools.embody.convoy.host'])
        self.assertEqual(
            install_mod.supervisor_argv('status', 'darwin', uid=501),
            ['launchctl', 'print', 'gui/501/tools.embody.convoy.host'])
        for argv in (install_mod.supervisor_argv('start', 'darwin',
                                                 uid=501),
                     install_mod.supervisor_argv('stop', 'darwin',
                                                 uid=501)):
            self.assertNotIn('system/', ' '.join(argv))

    def test_register_without_its_definition_is_refused(self):
        with self.assertRaises(ValueError):
            install_mod.supervisor_argv('register', 'win32')
        with self.assertRaises(ValueError):
            install_mod.supervisor_argv('register', 'darwin', uid=501)

    def test_an_unknown_action_is_refused_not_guessed(self):
        for platform in ('win32', 'darwin'):
            with self.assertRaises(ValueError):
                install_mod.supervisor_argv('destroy', platform, uid=501)


# -- 7. status parsing, against captured output ------------------------

# `schtasks /Query /TN EmbodyConvoyHost /FO LIST /V` output.
#
# PROVENANCE, STATED HONESTLY: the FIELD NAMES and the
# `Repeat: Every: N/A` line are from a real registered task on this
# machine (the 2026-07-31 registration probe -- that N/A is the whole
# reason parse_supervisor_status refuses to infer repetition). The
# surrounding rows are reconstructed from the documented /V format, NOT
# captured verbatim: an attempt to register a throwaway probe task from
# the build shell returned "Access is denied", because task registration
# needs the interactive session's token -- which is exactly the context
# Step 0 measured from INSIDE TouchDesigner, and does not contradict it.
#
# So: the PARSER's tolerance is what these tests prove, and they are
# written to survive extra or reordered rows. Confirming the exact
# byte-for-byte layout is a real-machine acceptance item (section 5),
# not something CI can close.
_SCHTASKS_RUNNING = """
Folder: \\
HostName:                             TEC-B4A
TaskName:                             \\EmbodyConvoyHost
Next Run Time:                        N/A
Status:                               Running
Logon Mode:                           Interactive only
Last Run Time:                        8/1/2026 12:31:00 PM
Last Result:                          267009
Author:                               TEC-B4A\\admin
Task To Run:                          C:\\...\\pythonw.exe "C:\\...\\launch.py"
Comment:                              Runs the Embody Convoy host app
Scheduled Task State:                 Enabled
Idle Time:                            Disabled
Power Management:
Run As User:                          admin
Delete Task If Not Rescheduled:       Disabled
Stop Task If Runs X Hours and X Mins: Disabled
Schedule:                             Scheduling data is not available
Schedule Type:                        At logon time
Start Time:                           N/A
Start Date:                           N/A
End Date:                             N/A
Days:                                 N/A
Months:                               N/A
Repeat: Every:                        N/A
Repeat: Until: Time:                  N/A
Repeat: Until: Duration:              N/A
Repeat: Stop If Still Running:        N/A
"""

_SCHTASKS_READY = _SCHTASKS_RUNNING.replace('Status:                       '
                                            '        Running',
                                            'Status:                       '
                                            '        Ready')
_SCHTASKS_DISABLED = (_SCHTASKS_RUNNING
                      .replace('Status:                               '
                               'Running',
                               'Status:                               '
                               'Disabled')
                      .replace('Scheduled Task State:                 '
                               'Enabled',
                               'Scheduled Task State:                 '
                               'Disabled'))
_SCHTASKS_MISSING = ('ERROR: The system cannot find the file specified.\n')

_LAUNCHCTL_RUNNING = """
tools.embody.convoy.host = {
\tactive count = 1
\tpath = /Users/x/Library/LaunchAgents/tools.embody.convoy.host.plist
\ttype = LaunchAgent
\tstate = running
\tpid = 4242
\tlast exit code = 0
\tprogram = /Applications/TouchDesigner.app/.../python3
}
"""

_LAUNCHCTL_MISSING = ('Could not find service "tools.embody.convoy.host" '
                      'in domain for gui\n')


class TestConvoyInstallStatusParsing(EmbodyTestCase):

    def test_the_repeat_every_NA_gotcha_never_reads_as_no_repetition(self):
        """MEASURED 2026-07-31, and the sharpest trap in this file.
        `schtasks /Query /FO LIST /V` prints `Repeat: Every: N/A` for a
        LOGON trigger EVEN WHEN the one-minute repetition IS stored --
        confirmed by exporting the same registered task with /XML and
        reading the Repetition element back. Concluding 'no repetition'
        from the human view would send someone re-registering a task
        that is working perfectly, so `repetition` is None (UNKNOWN FROM
        THIS VIEW) and must never be False."""
        self.assertIn('Repeat: Every:', _SCHTASKS_RUNNING)
        self.assertIn('N/A', _SCHTASKS_RUNNING)
        got = install_mod.parse_supervisor_status('win32',
                                                  _SCHTASKS_RUNNING)
        self.assertIsNone(got['repetition'])
        self.assertIsNot(got['repetition'], False)
        self.assertTrue(got['registered'])

    def test_a_running_task(self):
        got = install_mod.parse_supervisor_status('win32',
                                                  _SCHTASKS_RUNNING)
        self.assertTrue(got['registered'])
        self.assertEqual(got['state'], 'running')
        self.assertTrue(got['enabled'])
        self.assertEqual(got['last_result'], 267009)

    def test_a_ready_task_is_registered_and_enabled_but_not_running(self):
        got = install_mod.parse_supervisor_status('win32', _SCHTASKS_READY)
        self.assertTrue(got['registered'])
        self.assertEqual(got['state'], 'ready')
        self.assertTrue(got['enabled'])

    def test_a_disabled_task(self):
        got = install_mod.parse_supervisor_status('win32',
                                                  _SCHTASKS_DISABLED)
        self.assertTrue(got['registered'])
        self.assertEqual(got['state'], 'disabled')
        self.assertFalse(got['enabled'])

    def test_an_unregistered_task_arrives_on_STDERR_with_empty_stdout(self):
        """MEASURED: `schtasks /Query /TN <missing>` writes ZERO bytes to
        stdout and puts the error on STDERR with exit 1. The parser used
        to be handed only stdout, so it never reached its own
        not-registered branch in production -- it fell into 'the
        supervisor returned nothing' -- and the test that covered that
        branch was feeding stderr text into the stdout parameter,
        passing over a path the code could not enter."""
        got = install_mod.parse_supervisor_status(
            'win32', '', _SCHTASKS_MISSING, 1)
        self.assertFalse(got['registered'])
        self.assertFalse(got['query_failed'])
        self.assertIn('nothing is registered', got['detail'])

    def test_ACCESS_DENIED_is_not_reported_as_unregistered(self):
        """Both yield empty stdout, so without stderr they are
        indistinguishable -- and the UI would tell a user whose task is
        perfectly fine to run Install and repair it. 'We could not find
        out' is a different claim from 'there is nothing there'."""
        got = install_mod.parse_supervisor_status(
            'win32', '', 'ERROR: Access is denied.', 1)
        self.assertFalse(got['registered'])
        self.assertTrue(got['query_failed'])
        self.assertIn('access denied', got['detail'])

    def test_a_failed_query_never_becomes_no_supervisor(self):
        """host_state must not claim the supervisor is missing on
        evidence it does not have."""
        record = {'version': '6.0.171', 'supervisor': 'scheduled_task'}
        denied = install_mod.parse_supervisor_status(
            'win32', '', 'ERROR: Access is denied.', 1)
        got = install_mod.host_state(record, 'absent', denied, '6.0.171',
                                     True)
        self.assertNotEqual(got['state'], 'no_supervisor')
        self.assertEqual(got['state'], 'not_running')
        self.assertIn('access denied', got['detail'])
        # ...but a genuinely absent task still reads as no_supervisor.
        missing = install_mod.parse_supervisor_status(
            'win32', '', _SCHTASKS_MISSING, 1)
        self.assertEqual(
            install_mod.host_state(record, 'absent', missing, '6.0.171',
                                   True)['state'],
            'no_supervisor')

    def test_an_unregistered_agent_arrives_on_stderr_too(self):
        got = install_mod.parse_supervisor_status(
            'darwin', '', _LAUNCHCTL_MISSING, 113)
        self.assertFalse(got['registered'])
        self.assertFalse(got['query_failed'])

    def test_a_running_launch_agent(self):
        got = install_mod.parse_supervisor_status('darwin',
                                                  _LAUNCHCTL_RUNNING)
        self.assertTrue(got['registered'])
        self.assertEqual(got['state'], 'running')
        self.assertEqual(got['pid'], 4242)
        self.assertEqual(got['last_result'], 0)

    def test_an_unloaded_launch_agent(self):
        got = install_mod.parse_supervisor_status('darwin',
                                                  _LAUNCHCTL_MISSING)
        self.assertFalse(got['registered'])

    def test_it_tolerates_reordered_extra_and_respaced_rows(self):
        """The captured layout above is RECONSTRUCTED, not verbatim (see
        its provenance note), so the parser must not depend on row order,
        column alignment or the presence of any row it does not read.
        This is what makes the uncertainty survivable."""
        rows = [line for line in _SCHTASKS_RUNNING.splitlines()
                if line.strip()]
        shuffled = list(reversed(rows))
        shuffled.insert(3, 'Some Future Field:                    whatever')
        respaced = '\n'.join(' '.join(r.split()) for r in shuffled)
        got = install_mod.parse_supervisor_status('win32', respaced)
        self.assertTrue(got['registered'])
        self.assertEqual(got['state'], 'running')
        self.assertEqual(got['last_result'], 267009)
        self.assertIsNone(got['repetition'])

    def test_a_value_containing_a_colon_is_not_truncated(self):
        """'Task To Run: C:\\a\\b.exe' splits on the FIRST colon only --
        a naive split() would lose the drive letter."""
        got = install_mod.parse_supervisor_status(
            'win32', 'TaskName: \\X\nTask To Run: C:\\a\\b.exe\n'
                     'Status: Ready\n')
        self.assertTrue(got['registered'])
        self.assertEqual(got['state'], 'ready')

    def test_it_is_total_and_never_raises(self):
        """It parses whatever a supervisor happened to print, on a
        worker thread. A raise here kills the status refresh."""
        for platform in ('win32', 'darwin'):
            for junk in ('', '   ', None, 'garbage', '\x00\xff',
                         ':::::', 'a' * 10000, 'Last Result: not-a-number',
                         'TaskName: x\nLast Result: '):
                got = install_mod.parse_supervisor_status(platform, junk)
                self.assertIsInstance(got, dict)
                self.assertIn('registered', got)
                self.assertIsNone(got['repetition'])


# -- 8. isolated managed-runtime discovery -----------------------------

class TestConvoyInstallInterpreterDiscovery(EmbodyTestCase):

    def _runtime(self, root, runtime_id, platform, architecture,
                 python_version='3.11.15'):
        target = os.path.join(root, runtime_id)
        relative = ('python/pythonw.exe' if platform == 'win32'
                    else 'python/bin/python3')
        probe_relative = ('python/python.exe' if platform == 'win32'
                          else relative)
        interpreter = os.path.join(target, *relative.split('/'))
        os.makedirs(os.path.dirname(interpreter), exist_ok=True)
        with open(interpreter, 'wb') as f:
            f.write(b'fake managed python')
        probe_interpreter = os.path.join(target,
                                         *probe_relative.split('/'))
        if probe_interpreter != interpreter:
            with open(probe_interpreter, 'wb') as f:
                f.write(b'fake managed probe python')
        receipt = {
            'format': install_mod.RUNTIME_RECEIPT_FORMAT,
            'runtime_id': runtime_id,
            'platform': platform,
            'architecture': architecture,
            'python': relative,
            'probe_python': probe_relative,
            'python_version': python_version,
            'cryptography_version': 'test',
            'source_revision': 'test-fixture',
            'archive_sha256': 'a' * 64,
            'files': [],
        }
        seen = set()
        for name, path in ((relative, interpreter),
                           (probe_relative, probe_interpreter)):
            if name in seen:
                continue
            seen.add(name)
            with open(path, 'rb') as f:
                value = f.read()
            receipt['files'].append({
                'path': name,
                'size': len(value),
                'sha256': hashlib.sha256(value).hexdigest(),
                'mode': 0o755,
            })
        with open(os.path.join(target, install_mod.COMPLETE_FILE),
                  'w', encoding='utf-8') as f:
            json.dump(receipt, f)
        return interpreter

    def test_win32_discovery_finds_only_complete_managed_runtime(self):
        with _TempDir() as root:
            expected = self._runtime(root, 'runtime-1', 'win32', 'x86_64')
            found = install_mod.find_interpreters(
                'win32', roots=[root], architecture='AMD64')
            self.assertEqual([c['path'] for c in found], [expected])
            self.assertTrue(found[0]['managed'])
            self.assertTrue(found[0]['windowless'])

    def test_the_newest_managed_python_wins(self):
        with _TempDir() as root:
            self._runtime(root, 'runtime-old', 'win32', 'x86_64', '3.11.9')
            expected = self._runtime(root, 'runtime-new', 'win32', 'x86_64',
                                     '3.12.2')
            chosen = install_mod.choose_interpreter(
                install_mod.find_interpreters(
                    'win32', roots=[root], architecture='x86_64'))
            self.assertEqual(chosen, expected)

    def test_touchdesigner_and_unmanaged_python_are_never_candidates(self):
        with _TempDir() as root:
            binary = os.path.join(root, 'TouchDesigner.2025.33070', 'bin')
            os.makedirs(binary)
            with open(os.path.join(binary, 'pythonw.exe'), 'w') as f:
                f.write('')
            self.assertEqual(install_mod.find_interpreters(
                'win32', roots=[root], architecture='x86_64'), [])
            self.assertIsNone(install_mod.choose_interpreter([
                {'path': WIN_PY, 'windowless': True, 'managed': False}
            ]))

    def test_darwin_discovers_apple_silicon_runtime_only(self):
        with _TempDir() as root:
            expected = self._runtime(root, 'runtime-mac', 'darwin', 'arm64')
            found = install_mod.find_interpreters(
                'darwin', roots=[root], architecture='aarch64')
            self.assertEqual([c['path'] for c in found], [expected])
            self.assertEqual(install_mod.find_interpreters(
                'darwin', roots=[root], architecture='x86_64'), [])

    def test_an_empty_or_missing_root_yields_nothing_and_does_not_raise(self):
        with _TempDir() as root:
            self.assertEqual(
                install_mod.find_interpreters(
                    'win32', roots=[root], architecture='x86_64'), [])
        self.assertEqual(
            install_mod.find_interpreters(
                'win32', roots=['/definitely/not/here'],
                architecture='x86_64'), [])
        self.assertIsNone(install_mod.choose_interpreter([]))
        self.assertIsNone(install_mod.choose_interpreter(None))

    def test_an_incomplete_runtime_is_skipped(self):
        with _TempDir() as root:
            os.makedirs(os.path.join(root, 'runtime-no-receipt', 'python'))
            self.assertEqual(
                install_mod.find_interpreters(
                    'win32', roots=[root], architecture='x86_64'), [])

    def test_default_data_dir_matches_the_client_contract(self):
        self.assertEqual(install_mod.default_data_dir(
            'win32', env={'LOCALAPPDATA': r'C:\Users\x\AppData\Local'},
            home=r'C:\Users\x'), WIN_DATA)
        self.assertEqual(install_mod.default_data_dir(
            'darwin', env={}, home='/Users/x'), MAC_DATA)


class TestConvoyManagedRuntime(EmbodyTestCase):

    PLATFORM = 'win32'
    ARCH = 'x86_64'
    RUNTIME_ID = 'cpython-3.11.15-crypto-test-win64'
    PYTHON_REL = 'python/pythonw.exe'
    PROBE_PYTHON_REL = 'python/python.exe'
    CRYPTO_REL = 'Lib/site-packages/cryptography/__init__.py'

    def _payloads(self):
        return {
            self.PYTHON_REL: b'fake self-contained python',
            self.PROBE_PYTHON_REL: b'fake self-contained probe python',
            self.CRYPTO_REL: b'__version__ = "test"\n',
        }

    def _manifest(self, payloads=None, **changes):
        payloads = payloads or self._payloads()
        files = []
        for name in sorted(payloads):
            files.append({
                'path': name,
                'size': len(payloads[name]),
                'sha256': hashlib.sha256(payloads[name]).hexdigest(),
                'mode': (0o755 if name in (self.PYTHON_REL,
                                           self.PROBE_PYTHON_REL)
                         else 0o644),
            })
        manifest = {
            'format': install_mod.RUNTIME_BUNDLE_FORMAT,
            'runtime_id': self.RUNTIME_ID,
            'platform': self.PLATFORM,
            'architecture': self.ARCH,
            'python': self.PYTHON_REL,
            'probe_python': self.PROBE_PYTHON_REL,
            'python_version': '3.11.15',
            'cryptography_version': 'test',
            'source_revision': 'test-fixture',
            'files': files,
        }
        manifest.update(changes)
        return manifest

    def _bundle(self, root, manifest=None, payloads=None, extras=None,
                symlink=None):
        payloads = payloads or self._payloads()
        manifest = manifest or self._manifest(payloads)
        path = os.path.join(root, 'runtime.zip')
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(install_mod.RUNTIME_MANIFEST_FILE,
                             json.dumps(manifest))
            for name, value in payloads.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o100755 if name in (
                    self.PYTHON_REL, self.PROBE_PYTHON_REL)
                                      else 0o100644) << 16
                archive.writestr(info, value)
            for name, value in (extras or {}).items():
                archive.writestr(name, value)
            if symlink:
                info = zipfile.ZipInfo(symlink)
                info.external_attr = 0o120777 << 16
                archive.writestr(info, 'target')
        with open(path, 'rb') as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        return path, digest

    def _probe_runner(self, crypto_outside=False, base_outside=False,
                      fail=False):
        def run(argv, timeout_s=None):
            if fail:
                return 1, '', 'cryptography import failed'
            interpreter = os.path.realpath(argv[0])
            target = os.path.dirname(os.path.dirname(interpreter))
            crypto_file = (os.path.join(os.path.dirname(target), 'outside.py')
                           if crypto_outside else
                           os.path.join(target, *self.CRYPTO_REL.split('/')))
            result = {
                'format': install_mod.RUNTIME_PROBE_FORMAT,
                'implementation': 'CPython',
                'python': [3, 11, 15],
                'platform': self.PLATFORM,
                'architecture': 'AMD64',
                'executable': interpreter,
                'prefix': target,
                'base_prefix': (os.path.join(os.path.dirname(target),
                                             'system-python')
                                if base_outside else target),
                'stdlib': os.path.join(target, 'Lib'),
                'platstdlib': os.path.join(target, 'Lib'),
                'cryptography_version': 'test',
                'cryptography_file': crypto_file,
                'x509': True,
                'ed25519': True,
                'tls13': True,
            }
            return 0, json.dumps(result) + '\n', ''
        return run

    def _catalog(self, artifacts=None, win_status='published',
                 mac_status='release-asset-not-built'):
        return {
            'format': install_mod.RUNTIME_CATALOG_FORMAT,
            'artifacts': list(artifacts or []),
            'policy': {
                'network_install': False,
                'release_sha256_required': True,
                'windows_signing': 'Authenticode required before publication',
                'macos_signing': ('Developer ID and notarization required '
                                  'before publication'),
            },
            'required_targets': [
                {'platform': 'win32', 'architecture': 'x86_64',
                 'status': win_status},
                {'platform': 'darwin', 'architecture': 'arm64',
                 'status': mac_status},
            ],
        }

    def _artifact(self, bundle, digest, runtime_id=None, **changes):
        record = {
            'format': install_mod.RUNTIME_RELEASE_FORMAT,
            'runtime_id': runtime_id or self.RUNTIME_ID,
            'platform': self.PLATFORM,
            'architecture': self.ARCH,
            'asset': os.path.basename(bundle),
            'size': os.path.getsize(bundle),
            'sha256': digest,
            'python_version': '3.11.15',
            'cryptography_version': 'test',
            'source_revision': 'test-fixture',
            'status': 'published',
            'current': True,
            'signature': 'authenticode-verified',
        }
        record.update(changes)
        return record

    def test_supported_targets_are_windows_x64_and_apple_silicon(self):
        self.assertTrue(install_mod.runtime_target_supported(
            'win32', 'AMD64'))
        self.assertTrue(install_mod.runtime_target_supported(
            'darwin', 'aarch64'))
        self.assertFalse(install_mod.runtime_target_supported(
            'darwin', 'x86_64'))
        self.assertFalse(install_mod.runtime_target_supported(
            'linux', 'x86_64'))

    def test_missing_architecture_never_defaults_trusted_metadata_to_host(self):
        manifest = self._manifest()
        manifest.pop('architecture')
        got = install_mod._validate_runtime_manifest(
            manifest, self.PLATFORM, self.ARCH)
        self.assertEqual(got['reason'], 'runtime_target_mismatch')

        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            artifact = self._artifact(bundle, digest)
            artifact.pop('architecture')
            got = install_mod.validate_runtime_catalog(
                self._catalog([artifact]))
            self.assertEqual(got['reason'], 'runtime_catalog_invalid')

        ordinary = self._probe_runner()

        def missing_architecture(argv, timeout_s=None):
            code, out, err = ordinary(argv, timeout_s)
            value = json.loads(out)
            value.pop('architecture')
            return code, json.dumps(value), err

        got = install_mod.probe_runtime(
            '/managed/python', self.PLATFORM, self.ARCH,
            missing_architecture)
        self.assertEqual(got['reason'], 'runtime_probe_target_mismatch')

    def test_windows_runtime_separates_windowless_daemon_and_probe_python(self):
        manifest = self._manifest(python=self.PROBE_PYTHON_REL)
        got = install_mod._validate_runtime_manifest(
            manifest, self.PLATFORM, self.ARCH)
        self.assertFalse(got['ok'])
        self.assertIn('pythonw.exe', got['detail'])

    def test_release_pinned_bundle_provisions_and_verifies_offline(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner(), now=lambda: 12.5)
            self.assertTrue(got['ok'], got)
            self.assertFalse(got['current'])
            self.assertEqual(got['archive_sha256'], digest)
            receipt = install_mod.read_runtime_receipt(
                root, self.RUNTIME_ID, self.PLATFORM)
            self.assertEqual(receipt['format'],
                             install_mod.RUNTIME_RECEIPT_FORMAT)
            self.assertEqual(receipt['installed_at'], 12.5)
            self.assertTrue(os.path.isfile(got['interpreter']))

            verified = install_mod.verify_managed_runtime(
                root, got['interpreter'], self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(verified['ok'], verified)
            discovered = install_mod.find_interpreters(
                self.PLATFORM,
                roots=[os.path.join(root, install_mod.RUNTIME_SUBDIR)],
                architecture=self.ARCH)
            self.assertEqual([c['path'] for c in discovered],
                             [got['interpreter']])

    def test_catalog_selects_and_provisions_only_this_architecture_offline(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            catalog = self._catalog([self._artifact(bundle, digest)])
            catalog_path = os.path.join(root, 'runtime-catalog.json')
            with open(catalog_path, 'w', encoding='utf-8') as f:
                json.dump(catalog, f)
            selected = install_mod.select_runtime_artifact(
                catalog, self.PLATFORM, 'AMD64')
            self.assertTrue(selected['ok'], selected)
            self.assertEqual(selected['artifact']['runtime_id'],
                             self.RUNTIME_ID)
            installed = install_mod.provision_runtime_from_catalog(
                os.path.join(root, 'data'), catalog_path,
                platform=self.PLATFORM, architecture=self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(installed['ok'], installed)
            self.assertEqual(installed['runtime_id'], self.RUNTIME_ID)

    def test_catalog_selection_is_exact_for_windows_and_apple_silicon(self):
        win = {
            'format': install_mod.RUNTIME_RELEASE_FORMAT,
            'runtime_id': 'runtime-win',
            'platform': 'win32', 'architecture': 'x86_64',
            'asset': 'runtime-win.zip', 'size': 10, 'sha256': 'a' * 64,
            'python_version': '3.11.15', 'cryptography_version': '44.0.0',
            'source_revision': 'release-test', 'status': 'published',
            'current': True, 'signature': 'authenticode-verified',
        }
        mac = dict(win, runtime_id='runtime-mac', platform='darwin',
                   architecture='arm64', asset='runtime-mac.zip',
                   sha256='b' * 64,
                   signature='developer-id-notarized-verified')
        catalog = self._catalog([win, mac], mac_status='published')
        self.assertEqual(install_mod.select_runtime_artifact(
            catalog, 'win32', 'AMD64')['artifact']['runtime_id'],
            'runtime-win')
        self.assertEqual(install_mod.select_runtime_artifact(
            catalog, 'darwin', 'aarch64')['artifact']['runtime_id'],
            'runtime-mac')

    def test_empty_catalog_reports_the_external_release_gate_honestly(self):
        catalog = self._catalog([], win_status='release-asset-not-built')
        got = install_mod.select_runtime_artifact(
            catalog, self.PLATFORM, self.ARCH)
        self.assertFalse(got['ok'])
        self.assertEqual(got['reason'], 'runtime_bundle_unavailable')
        self.assertEqual(got['release_status'], 'release-asset-not-built')
        self.assertIn('TouchDesigner Python', got['detail'])
        self.assertIn('network installation', got['detail'])

    def test_catalog_rejects_candidate_or_unattested_release_metadata(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            candidate = self._artifact(
                bundle, digest, status='candidate', current=False,
                signature='verification-required')
            got = install_mod.validate_runtime_catalog(
                self._catalog([candidate], win_status='candidate'))
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'runtime_catalog_invalid')
            self.assertIn('published', got['detail'])

    def test_catalog_asset_path_and_runtime_id_are_release_fences(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            traversing = self._artifact(bundle, digest, asset='../runtime.zip')
            got = install_mod.validate_runtime_catalog(
                self._catalog([traversing]))
            self.assertEqual(got['reason'], 'runtime_catalog_invalid')

            wrong_id = self._artifact(bundle, digest,
                                      runtime_id='different-runtime-id')
            catalog = self._catalog([wrong_id])
            got = install_mod.provision_runtime_from_catalog(
                os.path.join(root, 'data'), catalog, asset_root=root,
                platform=self.PLATFORM, architecture=self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(got['reason'], 'runtime_catalog_mismatch')
            self.assertFalse(os.path.exists(os.path.join(
                root, 'data', install_mod.RUNTIME_SUBDIR, self.RUNTIME_ID)))

            wrong_provenance = self._artifact(
                bundle, digest, source_revision='different-release')
            got = install_mod.provision_runtime_from_catalog(
                os.path.join(root, 'data'),
                self._catalog([wrong_provenance]), asset_root=root,
                platform=self.PLATFORM, architecture=self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(got['reason'], 'runtime_catalog_mismatch')
            self.assertIn('source_revision', got['detail'])

    def test_catalog_size_mismatch_fails_before_archive_install(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            artifact = self._artifact(bundle, digest,
                                      size=os.path.getsize(bundle) + 1)
            got = install_mod.provision_runtime_from_catalog(
                os.path.join(root, 'data'), self._catalog([artifact]),
                asset_root=root, platform=self.PLATFORM,
                architecture=self.ARCH, runner=self._probe_runner())
            self.assertEqual(got['reason'], 'runtime_bundle_size_mismatch')
            self.assertFalse(os.path.exists(os.path.join(
                root, 'data', install_mod.RUNTIME_SUBDIR)))

    def test_fresh_runtime_appears_only_after_probe_and_atomic_activation(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            data = os.path.join(root, 'data')
            got = install_mod.provision_runtime_bundle(
                data, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner(fail=True))
            self.assertEqual(got['reason'], 'runtime_probe_failed')
            runtime_root = os.path.join(data, install_mod.RUNTIME_SUBDIR)
            self.assertFalse(os.path.exists(os.path.join(
                runtime_root, self.RUNTIME_ID)))
            if os.path.isdir(runtime_root):
                self.assertFalse(any(name.startswith('.install-')
                                     for name in os.listdir(runtime_root)))

    def test_concurrent_identical_activation_converges_on_verified_winner(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            real_replace = os.replace
            raced = []

            def winner_then_race(source, destination):
                if (not raced
                        and os.path.basename(source).startswith('.install-')):
                    raced.append(True)
                    real_replace(source, destination)
                    raise PermissionError('simulated concurrent activation')
                return real_replace(source, destination)

            os.replace = winner_then_race
            try:
                got = install_mod.provision_runtime_bundle(
                    root, bundle, digest, self.PLATFORM, self.ARCH,
                    runner=self._probe_runner())
            finally:
                os.replace = real_replace
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['current'])
            self.assertTrue(raced)

    def test_incomplete_existing_runtime_is_never_overwritten(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            target = os.path.join(root, install_mod.RUNTIME_SUBDIR,
                                  self.RUNTIME_ID)
            os.makedirs(target)
            stranger = os.path.join(target, 'keep.txt')
            with open(stranger, 'w') as f:
                f.write('unknown ownership')
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(got['reason'], 'runtime_incomplete_exists')
            self.assertTrue(os.path.isfile(stranger))

    def test_portable_case_collisions_and_windows_devices_are_refused(self):
        payloads = self._payloads()
        payloads['Lib/A.py'] = b'a'
        payloads['lib/a.py'] = b'b'
        got = install_mod._validate_runtime_manifest(
            self._manifest(payloads), self.PLATFORM, self.ARCH)
        self.assertEqual(got['reason'], 'runtime_manifest_invalid')
        self.assertIn('collide', got['detail'])

        payloads = self._payloads()
        payloads['Lib/NUL.dll'] = b'bad'
        got = install_mod._validate_runtime_manifest(
            self._manifest(payloads), self.PLATFORM, self.ARCH)
        self.assertEqual(got['reason'], 'runtime_manifest_invalid')
        self.assertIn('unsafe', got['detail'])

        payloads = self._payloads()
        payloads['Lib/conflict'] = b'file'
        payloads['lib/conflict/module.py'] = b'child'
        got = install_mod._validate_runtime_manifest(
            self._manifest(payloads), self.PLATFORM, self.ARCH)
        self.assertEqual(got['reason'], 'runtime_manifest_invalid')
        self.assertIn('parent directory', got['detail'])

    def test_verification_hashes_native_dependencies_not_only_python(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            installed = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            crypto = os.path.join(
                root, install_mod.RUNTIME_SUBDIR, self.RUNTIME_ID,
                *self.CRYPTO_REL.split('/'))
            with open(crypto, 'ab') as f:
                f.write(b'tampered')
            verified = install_mod.verify_managed_runtime(
                root, installed['interpreter'], self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(verified['reason'], 'runtime_integrity_failed')
            self.assertIn(self.CRYPTO_REL, verified['detail'])

    def test_provision_is_idempotent_but_runtime_id_collision_refuses(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            first = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            second = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(first['ok'] and second['ok'])
            self.assertTrue(second['current'])
            receipt_path = os.path.join(
                root, install_mod.RUNTIME_SUBDIR, self.RUNTIME_ID,
                install_mod.COMPLETE_FILE)
            with open(receipt_path, encoding='utf-8') as f:
                receipt = json.load(f)
            receipt['archive_sha256'] = 'b' * 64
            with open(receipt_path, 'w', encoding='utf-8') as f:
                json.dump(receipt, f)
            refused = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(refused['reason'], 'runtime_id_collision')

    def test_same_release_bundle_repairs_a_damaged_runtime(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            first = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            with open(first['interpreter'], 'wb') as f:
                f.write(b'damaged')
            repaired = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(repaired['ok'], repaired)
            self.assertFalse(repaired['current'])
            expected = next(row['sha256'] for row in self._manifest()['files']
                            if row['path'] == self.PYTHON_REL)
            with open(repaired['interpreter'], 'rb') as f:
                actual = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(actual, expected)

    def test_interrupted_same_release_repair_can_resume_safely(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            first = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            with open(first['interpreter'], 'wb') as f:
                f.write(b'damaged')
            interrupted = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner(fail=True))
            self.assertEqual(interrupted['reason'], 'runtime_probe_failed')
            self.assertIsNone(install_mod.read_runtime_receipt(
                root, self.RUNTIME_ID, self.PLATFORM))
            resumed = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(resumed['ok'], resumed)
            self.assertIsNotNone(install_mod.read_runtime_receipt(
                root, self.RUNTIME_ID, self.PLATFORM))

    def test_missing_or_wrong_release_digest_writes_nothing(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            for supplied, reason in ((None, 'runtime_digest_required'),
                                     ('0' * 64,
                                      'runtime_bundle_digest_mismatch')):
                got = install_mod.provision_runtime_bundle(
                    root, bundle, supplied, self.PLATFORM, self.ARCH,
                    runner=self._probe_runner())
                self.assertEqual(got['reason'], reason)
            self.assertFalse(os.path.exists(os.path.join(
                root, install_mod.RUNTIME_SUBDIR)))
            self.assertNotEqual(digest, '0' * 64)

    def test_extra_traversing_and_symlink_members_are_refused(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root, extras={'extra.dll': b'x'})
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(got['reason'], 'runtime_bundle_invalid')

        with _TempDir() as root:
            payloads = self._payloads()
            payloads['../escape.py'] = b'bad'
            bundle, digest = self._bundle(
                root, manifest=self._manifest(payloads), payloads=payloads)
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(got['reason'], 'runtime_manifest_invalid')
            self.assertFalse(os.path.exists(os.path.join(root, 'escape.py')))

        with _TempDir() as root:
            payloads = self._payloads()
            payloads['link'] = b'target'
            bundle, digest = self._bundle(
                root, manifest=self._manifest(payloads), payloads={
                    key: value for key, value in payloads.items()
                    if key != 'link'
                }, symlink='link')
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(got['reason'], 'runtime_bundle_invalid')

    def test_failed_or_external_crypto_probe_never_writes_complete(self):
        for runner, reason in (
                (self._probe_runner(fail=True), 'runtime_probe_failed'),
                (self._probe_runner(crypto_outside=True),
                 'runtime_dependency_outside_bundle'),
                (self._probe_runner(base_outside=True),
                 'runtime_dependency_outside_bundle')):
            with _TempDir() as root:
                bundle, digest = self._bundle(root)
                got = install_mod.provision_runtime_bundle(
                    root, bundle, digest, self.PLATFORM, self.ARCH,
                    runner=runner)
                self.assertEqual(got['reason'], reason)
                self.assertIsNone(install_mod.read_runtime_receipt(
                    root, self.RUNTIME_ID, self.PLATFORM))

    def test_manifest_versions_must_match_the_live_runtime(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(
                root, manifest=self._manifest(
                    cryptography_version='different'))
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertEqual(got['reason'],
                             'runtime_manifest_probe_mismatch')
            self.assertIsNone(install_mod.read_runtime_receipt(
                root, self.RUNTIME_ID, self.PLATFORM))

    def test_runtime_uninstall_removes_only_receipt_listed_files(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            installed = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(installed['ok'], installed)
            plan = install_mod.plan_runtime_uninstall(root)
            self.assertEqual(plan['runtime_ids'], [self.RUNTIME_ID])
            self.assertIn(installed['interpreter'], plan['remove'])
            removed = install_mod.remove_managed_runtime(
                root, self.RUNTIME_ID)
            self.assertTrue(removed['removed_dir'], removed)
            self.assertFalse(os.path.exists(os.path.join(
                root, install_mod.RUNTIME_SUBDIR, self.RUNTIME_ID)))

    def test_runtime_uninstall_keeps_and_reports_a_stranger(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            installed = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            target = os.path.dirname(os.path.dirname(installed['interpreter']))
            stranger = os.path.join(target, 'user-file.txt')
            with open(stranger, 'w') as f:
                f.write('keep')
            removed = install_mod.remove_managed_runtime(
                root, self.RUNTIME_ID)
            self.assertFalse(removed['removed_dir'])
            self.assertIn(stranger, removed['remaining'])
            self.assertTrue(os.path.isfile(stranger))

    def test_runtime_uninstall_never_follows_a_redirected_runtime_dir(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            installed = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            target = os.path.dirname(os.path.dirname(installed['interpreter']))
            ordinary_islink = os.path.islink

            def redirected(path):
                return (os.path.normcase(os.path.abspath(path))
                        == os.path.normcase(os.path.abspath(target))
                        or ordinary_islink(path))

            with mock.patch.object(install_mod.os.path, 'islink',
                                   side_effect=redirected):
                plan = install_mod.plan_runtime_uninstall(root)
                removed = install_mod.remove_managed_runtime(
                    root, self.RUNTIME_ID)
            self.assertIn(target, plan['stray'])
            self.assertFalse(removed['removed_dir'])
            self.assertIn(target, removed['kept'])
            self.assertTrue(os.path.isfile(installed['interpreter']))

    def test_incomplete_runtime_is_never_deleted_by_uninstall(self):
        with _TempDir() as root:
            target = os.path.join(root, install_mod.RUNTIME_SUBDIR,
                                  'interrupted-runtime')
            os.makedirs(target)
            unknown = os.path.join(target, 'unknown.bin')
            with open(unknown, 'wb') as f:
                f.write(b'user-or-interrupted-data')
            plan = install_mod.plan_runtime_uninstall(root)
            self.assertIn(target, plan['incomplete'])
            got = install_mod.remove_managed_runtime(
                root, 'interrupted-runtime')
            self.assertFalse(got['removed_dir'])
            self.assertTrue(os.path.isfile(unknown))

    def test_host_uninstall_removes_the_complete_managed_runtime(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            installed = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(installed['ok'], installed)
            got = install_mod.uninstall(
                root, platform=self.PLATFORM, runner=_Runner())
            self.assertTrue(got['ok'], got)
            self.assertFalse(os.path.exists(os.path.join(
                root, install_mod.RUNTIME_SUBDIR, self.RUNTIME_ID)))

    def test_probe_requires_cpython_crypto_ed25519_and_tls13(self):
        bad = self._probe_runner()

        def missing_tls(argv, timeout_s=None):
            code, out, err = bad(argv, timeout_s)
            value = json.loads(out)
            value['tls13'] = False
            return code, json.dumps(value), err

        got = install_mod.probe_runtime(
            '/managed/python', self.PLATFORM, self.ARCH, missing_tls)
        self.assertEqual(got['reason'], 'runtime_crypto_unavailable')

    def test_install_refuses_unmanaged_python_before_writing_anything(self):
        with _TempDir() as root:
            got = install_mod.install(
                root, '6.0.171', _MODULES, WIN_PY, platform='win32',
                runner=_Runner(), user=WIN_USER, architecture='x86_64')
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'runtime_not_managed')
            self.assertIn('TouchDesigner Python', got['detail'])
            self.assertFalse(os.path.exists(
                install_mod.app_dir(root, platform='win32')))
            self.assertIsNone(install_mod.read_installed(root, 'win32'))

    def test_installer_accepts_a_provisioned_runtime_and_records_provenance(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            runtime = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            supervisor = _Runner()
            got = install_mod.install(
                root, '6.0.171', _MODULES, runtime['interpreter'],
                platform=self.PLATFORM, architecture=self.ARCH,
                runner=supervisor, runtime_runner=self._probe_runner(),
                user=WIN_USER)
            self.assertTrue(got['ok'], got)
            record = install_mod.read_installed(root, self.PLATFORM)
            self.assertEqual(record['runtime']['runtime_id'], self.RUNTIME_ID)
            self.assertEqual(record['runtime']['archive_sha256'], digest)
            self.assertEqual(record['runtime']['cryptography_version'], 'test')
            self.assertEqual(record['runtime']['source_revision'],
                             'test-fixture')
            self.assertEqual(len(supervisor.calls), 1)

    def test_fresh_machine_install_can_provision_from_local_catalog(self):
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            catalog = self._catalog([self._artifact(bundle, digest)])
            supervisor = _Runner()
            data = os.path.join(root, 'data')
            got = install_mod.install(
                data, '6.0.171', _MODULES, None,
                platform=self.PLATFORM, architecture=self.ARCH,
                runner=supervisor, runtime_runner=self._probe_runner(),
                runtime_catalog=catalog, runtime_asset_root=root,
                user=WIN_USER)
            self.assertTrue(got['ok'], got)
            self.assertEqual(got['record']['runtime']['runtime_id'],
                             self.RUNTIME_ID)
            self.assertEqual(len(supervisor.calls), 1)

    def test_fresh_machine_install_with_empty_catalog_writes_nothing(self):
        with _TempDir() as root:
            data = os.path.join(root, 'data')
            got = install_mod.install(
                data, '6.0.171', _MODULES, None,
                platform=self.PLATFORM, architecture=self.ARCH,
                runner=_Runner(), runtime_runner=self._probe_runner(),
                runtime_catalog=self._catalog(
                    [], win_status='release-asset-not-built'),
                runtime_asset_root=root, user=WIN_USER)
            self.assertEqual(got['reason'], 'runtime_bundle_unavailable')
            self.assertIn('TouchDesigner Python', got['detail'])
            self.assertFalse(os.path.exists(
                install_mod.app_dir(data, platform=self.PLATFORM)))
            self.assertIsNone(install_mod.read_installed(data, self.PLATFORM))

    def test_runtime_catalog_declares_both_unbuilt_release_assets(self):
        path = os.path.join(_CONVOY_DIR, 'convoy_runtime_catalog.json')
        with open(path, encoding='utf-8') as f:
            catalog = json.load(f)
        self.assertEqual(catalog['format'],
                         'embody-convoy-runtime-catalog/1')
        targets = {(row['platform'], row['architecture'], row['status'])
                   for row in catalog['required_targets']}
        self.assertEqual(targets, {
            ('win32', 'x86_64', 'release-asset-not-built'),
            ('darwin', 'arm64', 'release-asset-not-built'),
        })
        self.assertEqual(catalog['artifacts'], [])
        self.assertFalse(catalog['policy']['network_install'])
        self.assertTrue(catalog['policy']['release_sha256_required'])
        self.assertIn('Authenticode', catalog['policy']['windows_signing'])
        self.assertIn('notarization', catalog['policy']['macos_signing'])
        self.assertEqual(catalog['artifact_contract']['builder_output_status'],
                         'candidate')
        self.assertEqual(catalog['artifact_contract']['windows_signature'],
                         'authenticode-verified')
        self.assertEqual(catalog['artifact_contract']['macos_signature'],
                         'developer-id-notarized-verified')
        checked = install_mod.read_runtime_catalog(path)
        self.assertTrue(checked['ok'], checked)
        for platform_name, architecture in (('win32', 'x86_64'),
                                             ('darwin', 'arm64')):
            unavailable = install_mod.select_runtime_artifact(
                checked['catalog'], platform_name, architecture)
            self.assertEqual(unavailable['reason'],
                             'runtime_bundle_unavailable')

    def test_runtime_packager_has_no_download_path(self):
        path = os.path.join(_CONVOY_DIR, 'build_convoy_runtime_bundle.py')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        tree = ast.parse(source, path)
        imports = _imported_modules(tree)
        for forbidden in ('requests', 'urllib', 'httpx', 'http', 'socket'):
            self.assertNotIn(forbidden, {name.split('.')[0]
                                         for name in imports})
        for forbidden_call in ('urlopen(', 'requests.', 'curl', 'wget'):
            self.assertNotIn(forbidden_call, source)

    def test_runtime_packager_output_is_consumed_by_offline_installer(self):
        platform_name = sys.platform
        architecture = install_mod.normalize_architecture()
        if not install_mod.runtime_target_supported(
                platform_name, architecture):
            self.skipTest('host is not a supported runtime-build target')
        if platform_name == 'win32':
            python_rel = 'python/pythonw.exe'
            probe_rel = 'python/python.exe'
            stdlib_rel = 'Lib'
        else:
            python_rel = probe_rel = 'python/bin/python3'
            stdlib_rel = 'python/lib/python3.11'
        crypto_rel = 'python/site-packages/cryptography/__init__.py'

        with _TempDir() as root:
            prepared = os.path.join(root, 'prepared')
            payloads = {
                python_rel: b'daemon-python',
                probe_rel: b'probe-python',
                crypto_rel: b'__version__ = "test"\n',
                os.path.join(stdlib_rel, 'os.py').replace('\\', '/'):
                    b'# stdlib marker\n',
            }
            for relative, value in payloads.items():
                path = os.path.join(prepared, *relative.split('/'))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(value)
                if relative in (python_rel, probe_rel):
                    os.chmod(path, 0o755)

            def probe(argv, timeout_s=None):
                target = os.path.realpath(argv[0])
                for unused in probe_rel.split('/'):
                    target = os.path.dirname(target)
                result = {
                    'format': install_mod.RUNTIME_PROBE_FORMAT,
                    'implementation': 'CPython',
                    'python': [3, 11, 15],
                    'platform': platform_name,
                    'architecture': architecture,
                    'executable': os.path.realpath(argv[0]),
                    'prefix': target,
                    'base_prefix': target,
                    'stdlib': os.path.join(target,
                                            *stdlib_rel.split('/')),
                    'platstdlib': os.path.join(target,
                                                *stdlib_rel.split('/')),
                    'cryptography_version': 'test',
                    'cryptography_file': os.path.join(
                        target, *crypto_rel.split('/')),
                    'x509': True,
                    'ed25519': True,
                    'tls13': True,
                }
                return 0, json.dumps(result), ''

            spec = importlib.util.spec_from_file_location(
                'convoy_runtime_builder_test', _RUNTIME_BUILDER_PATH)
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            output = os.path.join(root, 'runtime.zip')
            metadata = builder.build_bundle(
                prepared, output, 'runtime-packager-test', python_rel,
                probe_rel, platform_name, architecture, 'test-revision',
                installer=install_mod, probe_runner=probe)
            second_output = os.path.join(root, 'runtime-second.zip')
            second_metadata = builder.build_bundle(
                prepared, second_output, 'runtime-packager-test', python_rel,
                probe_rel, platform_name, architecture, 'test-revision',
                installer=install_mod, probe_runner=probe)
            with open(output, 'rb') as f:
                output_sha = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(metadata['sha256'], output_sha)
            self.assertEqual(second_metadata['sha256'], output_sha)
            self.assertEqual(metadata['status'], 'candidate')
            self.assertEqual(metadata['signature'], 'verification-required')
            self.assertFalse(metadata['current'])
            self.assertFalse(install_mod.validate_runtime_catalog(
                self._catalog([metadata], win_status='candidate')
                if platform_name == 'win32' else {
                    **self._catalog([], mac_status='candidate'),
                    'artifacts': [metadata],
                })['ok'])
            installed = install_mod.provision_runtime_bundle(
                os.path.join(root, 'data'), output, metadata['sha256'],
                platform_name, architecture, runner=probe)
            self.assertTrue(installed['ok'], installed)
            self.assertEqual(installed['runtime_id'],
                             'runtime-packager-test')


# -- 8b. probe failure diagnostics ---------------------------------------

class TestConvoyProbeFailureDiagnostics(EmbodyTestCase):
    """The dlopen reason must SURVIVE to the log, and be classified.

    The 2026-08-04 macOS field failure was diagnosed blind twice: the
    probe's stderr was head-truncated at 500 chars (losing the final
    'ImportError: dlopen(...) incompatible architecture' line), and the
    aggregate install message then dropped the detail entirely. These
    tests pin the tail-preserving clip and the missing-vs-broken
    cryptography classification that ended that blindness.
    """

    SITE = ('/Users/rosco/Desktop/e1/.venv/lib/python3.11/site-packages/'
            'cryptography')

    def _dlopen_traceback(self):
        frames = ''.join(
            '  File "%s/x509/frame_%d.py", line %d, in <module>\n'
            '    from cryptography.hazmat.bindings._rust import x509\n'
            % (self.SITE, n, n) for n in range(12))
        return (
            'Traceback (most recent call last):\n' + frames +
            "ImportError: dlopen(%s/hazmat/bindings/_rust.abi3.so, 0x0002):"
            " tried: '%s/hazmat/bindings/_rust.abi3.so' (mach-o file, but "
            "is an incompatible architecture (have 'arm64', need "
            "'x86_64'))" % (self.SITE, self.SITE))

    def test_missing_cryptography_is_classified(self):
        for stderr in (
                "ModuleNotFoundError: No module named 'cryptography'",
                "Traceback ...\nModuleNotFoundError: No module named "
                "'cryptography'"):
            self.assertEqual(
                install_mod.classify_probe_failure(stderr),
                'runtime_missing_cryptography')

    def _signature_traceback(self):
        # The verified field shape (arm64 Mac, 2026-08-04): TouchDesigner's
        # library-validation-signed python refusing the foreign-signed wheel.
        return (
            'Traceback (most recent call last):\n'
            '  File "<string>", line 8, in <module>\n'
            "ImportError: dlopen(%s/hazmat/bindings/_rust.abi3.so, 0x0002):"
            " tried: '%s/hazmat/bindings/_rust.abi3.so' (code signature in "
            "<65827F6F> '%s/hazmat/bindings/_rust.abi3.so' not valid for "
            'use in process: mapping process and mapped file (non-platform)'
            ' have different Team IDs)' % (self.SITE, self.SITE, self.SITE))

    def test_signature_refusal_is_classified_before_broken(self):
        """The Team-ID text also contains '_rust' -- signature must win,
        because the two classes need OPPOSITE responses (a reinstall can
        fix broken; nothing package-level can fix signature policy)."""
        self.assertEqual(
            install_mod.classify_probe_failure(self._signature_traceback()),
            'runtime_crypto_signature_blocked')
        # The ad-hoc variant macOS emits for unsigned dylibs.
        self.assertEqual(
            install_mod.classify_probe_failure(
                'ImportError: dlopen(.../cryptography/hazmat/bindings/'
                '_rust.abi3.so): code signature not valid for use in '
                'process using Library Validation: mapped file has no '
                'Team ID and is not a platform binary'),
            'runtime_crypto_signature_blocked')

    def test_signature_refusal_needs_cryptography_context(self):
        self.assertEqual(
            install_mod.classify_probe_failure(
                'dlopen(libwhatever.dylib): code signature not valid for '
                'use in process: different Team IDs'),
            'runtime_probe_failed')

    def test_probe_keeps_the_team_id_tail_end_to_end(self):
        text = self._signature_traceback()

        def runner(argv, timeout_s=None):
            return 1, '', text

        got = install_mod.probe_runtime('/venv/bin/python', 'darwin',
                                        'arm64', runner)
        self.assertEqual(got['reason'], 'runtime_crypto_signature_blocked')
        self.assertIn('different Team IDs', got['detail'])
        snippet = install_mod.probe_detail_snippet(got['detail'])
        self.assertIn('Team ID', snippet)

    def test_broken_cryptography_is_classified(self):
        self.assertEqual(
            install_mod.classify_probe_failure(self._dlopen_traceback()),
            'runtime_crypto_broken')
        self.assertEqual(
            install_mod.classify_probe_failure(
                'ImportError: something about _rust bindings'),
            'runtime_crypto_broken')
        # A half-installed wheel: the SUBMODULE is missing, the package
        # is present -- that is broken, not absent.
        self.assertEqual(
            install_mod.classify_probe_failure(
                "ModuleNotFoundError: No module named "
                "'cryptography.hazmat.bindings._rust'"),
            'runtime_crypto_broken')

    def test_unrecognized_failures_keep_the_generic_reason(self):
        # Including the historical fixture string other suites rely on,
        # and a dlopen failure with no cryptography context -- 'rebuild
        # the venv' guidance cannot help an unrelated dylib.
        for stderr in ('cryptography import failed', '', 'Segmentation '
                       'fault', None,
                       'OSError: dlopen(libssl.dylib): image not found'):
            self.assertEqual(install_mod.classify_probe_failure(stderr),
                             'runtime_probe_failed')

    def test_probe_detail_keeps_the_dlopen_tail(self):
        text = self._dlopen_traceback()
        self.assertGreater(len(text), 1600, 'fixture must force clipping')

        def runner(argv, timeout_s=None):
            return 1, '', text

        got = install_mod.probe_runtime('/venv/bin/python', 'darwin',
                                        'arm64', runner)
        self.assertFalse(got['ok'])
        self.assertEqual(got['reason'], 'runtime_crypto_broken')
        self.assertLessEqual(len(got['detail']), 1600)
        self.assertTrue(got['detail'].startswith('Traceback'),
                        'the head marker must survive the clip')
        self.assertIn("incompatible architecture (have 'arm64', need "
                      "'x86_64')", got['detail'],
                      'the FINAL line is the diagnosis and must survive')

    def test_probe_missing_cryptography_reason(self):
        def runner(argv, timeout_s=None):
            return 1, '', ("Traceback (most recent call last):\n"
                           '  File "<string>", line 6, in <module>\n'
                           "ModuleNotFoundError: No module named "
                           "'cryptography'")

        got = install_mod.probe_runtime('/usr/bin/python3', 'darwin',
                                        'arm64', runner)
        self.assertEqual(got['reason'], 'runtime_missing_cryptography')
        self.assertIn("No module named 'cryptography'", got['detail'])

    def test_clip_detail_keeps_both_ends(self):
        short = 'a short message'
        self.assertEqual(install_mod._clip_detail(short), short)
        long_text = 'HEADSTART ' + ('x' * 4000) + ' TAILEND'
        clipped = install_mod._clip_detail(long_text)
        self.assertLessEqual(len(clipped), 1600)
        self.assertIn('HEADSTART', clipped)
        self.assertIn('TAILEND', clipped)
        self.assertIn(' ... ', clipped)
        # A limit smaller than the head window must still be honored,
        # never exceeded (the naive arithmetic returned MORE than asked).
        tiny = install_mod._clip_detail('B' * 300, limit=100)
        self.assertLessEqual(len(tiny), 100)

    def test_snippet_prefers_the_last_line(self):
        self.assertEqual(
            install_mod.probe_detail_snippet('one\ntwo\nfinal diagnosis'),
            'final diagnosis')
        self.assertEqual(install_mod.probe_detail_snippet(''), '')

    def test_snippet_centers_on_the_diagnosis_marker(self):
        snippet = install_mod.probe_detail_snippet(self._dlopen_traceback())
        self.assertLessEqual(len(snippet), 240)
        self.assertIn('incompatible architecture', snippet)

    def test_snippet_falls_back_to_the_line_head(self):
        line = 'Z' * 400
        snippet = install_mod.probe_detail_snippet('context\n' + line)
        self.assertTrue(snippet.startswith('Z'))
        self.assertTrue(snippet.endswith('...'))
        self.assertLessEqual(len(snippet), 240)


# -- 9. plan_install matrix ---------------------------------------------

class TestConvoyInstallPlan(EmbodyTestCase):

    def _record(self, version, supervisor='scheduled_task'):
        return {'version': version, 'supervisor': supervisor}

    def test_fresh_machine(self):
        got = install_mod.plan_install(None, '6.0.171')
        self.assertEqual(got['action'], 'install')

    def test_same_version_is_a_stated_no_op(self):
        """A-36: a second project pulsing Install must be honest that
        nothing happened, not silently reinstall."""
        got = install_mod.plan_install(self._record('6.0.171'), '6.0.171')
        self.assertEqual(got['action'], 'current')
        self.assertIn('already installed', got['detail'])

    def test_ours_newer_upgrades(self):
        got = install_mod.plan_install(self._record('6.0.171'), '6.0.180')
        self.assertEqual(got['action'], 'upgrade')

    def test_ours_older_REFUSES(self):
        """A newer host app must never be downgraded by an older
        project. 171 vs 180 is also the case a string compare gets
        backwards ('6.0.171' > '6.0.180' lexically)."""
        got = install_mod.plan_install(self._record('6.0.180'), '6.0.171')
        self.assertEqual(got['action'], 'refuse_downgrade')
        self.assertIn('6.0.180', got['detail'])

    def test_numeric_not_lexical_ordering(self):
        self.assertEqual(
            install_mod.plan_install(self._record('6.0.9'),
                                     '6.0.171')['action'], 'upgrade')
        self.assertEqual(
            install_mod.plan_install(self._record('6.0.171'),
                                     '6.0.9')['action'], 'refuse_downgrade')

    def test_an_external_supervisor_is_never_taken_over(self):
        """A-36's escape hatch: write the payload, register NOTHING.
        Never two supervisors."""
        got = install_mod.plan_install(
            self._record('6.0.171', 'external'), '6.0.180')
        self.assertEqual(got['action'], 'external')
        self.assertIn('another supervisor', got['detail'])

    def test_external_does_NOT_authorise_a_downgrade(self):
        """Ordering matters: 'someone else supervises it' is not
        permission to replace a newer host app with an older one."""
        got = install_mod.plan_install(
            self._record('6.0.180', 'external'), '6.0.171')
        self.assertEqual(got['action'], 'refuse_downgrade')

    def test_a_corrupt_record_becomes_an_upgrade_not_a_refusal(self):
        """Install is also the repair path -- refusing on an unreadable
        record would leave the user with no way forward.

        REGRESSION GUARD, MEASURED 2026-08-01: _version_key used to rank
        a non-numeric chunk ABOVE every numeric one, so ANY unparseable
        version in installed.json read as NEWER than ours and wedged
        Install into refuse_downgrade PERMANENTLY, with no route out
        from the UI -- the user had to hand-delete installed.json. That
        was the exact inverse of this method's documented promise."""
        for broken in ({'supervisor': 'scheduled_task'},
                       {'version': ''}, {'version': None},
                       {'version': 'garbage'}, {'version': 'dev'},
                       {'version': 'HEAD'}, {'version': 'latest'},
                       {'version': 'v6.0.171'},
                       {'version': '6.0.171-rc1'},
                       {'version': '6.0.171+dirty'},
                       {'version': {'a': 1}}, {'version': [1]},
                       {'version': True}):
            self.assertEqual(
                install_mod.plan_install(broken, '6.0.171')['action'],
                'upgrade', 'installed=%r wedged Install' % (broken,))

    def test_an_unorderable_version_of_OUR_OWN_still_installs(self):
        """UNUSABLE-AS-A-PATH and UNORDERABLE are different failures.
        '6.0.171-rc1' is a perfectly good directory name that simply
        cannot be ranked against '6.0.171'; blocking an rc build from
        installing itself would be absurd."""
        self.assertEqual(install_mod.plan_install(None,
                                                  '6.0.171-rc1')['action'],
                         'install')
        self.assertEqual(
            install_mod.plan_install(self._record('6.0.171'),
                                     '6.0.171-rc1')['action'], 'upgrade')

    def test_no_usable_version_of_our_own_refuses(self):
        """Only a version that cannot become a DIRECTORY NAME refuses --
        nothing can be written for it."""
        for bad in (None, '', '   ', '../evil', 'a/b', 'D:x'):
            got = install_mod.plan_install(self._record('6.0.171'), bad)
            self.assertEqual(got['action'], 'refuse_downgrade',
                             'ours=%r' % (bad,))

    def test_a_REAL_downgrade_is_still_refused(self):
        """The loosening above must not have opened the door it exists
        to keep shut."""
        self.assertEqual(
            install_mod.plan_install(self._record('6.0.180'),
                                     '6.0.171')['action'],
            'refuse_downgrade')
        self.assertEqual(
            install_mod.plan_install(self._record('7.0.0'),
                                     '6.9.9')['action'],
            'refuse_downgrade')

    def test_it_never_raises(self):
        for installed in (None, {}, [], 'string', 42,
                          {'version': ['weird']}):
            got = install_mod.plan_install(installed, '6.0.171')
            self.assertIn('action', got)


class TestConvoyInstallRecord(EmbodyTestCase):

    def test_write_then_read_round_trips(self):
        with _TempDir() as root:
            install_mod.write_installed(root, {'version': '6.0.171',
                                               'supervisor': 'scheduled_task'})
            got = install_mod.read_installed(root)
            self.assertEqual(got['version'], '6.0.171')

    def test_a_corrupt_record_reads_as_absent_never_raises(self):
        with _TempDir() as root:
            path = install_mod.installed_path(root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            for junk in ('{not json', '', 'null', '[1,2]', '"a string"'):
                with open(path, 'w') as f:
                    f.write(junk)
                self.assertIsNone(install_mod.read_installed(root))

    def test_an_absent_record_reads_as_none(self):
        with _TempDir() as root:
            self.assertIsNone(install_mod.read_installed(root))

    def test_the_record_is_written_atomically(self):
        with _TempDir() as root:
            install_mod.write_installed(root, {'version': '6.0.171'})
            leftovers = [n for n in os.listdir(root) if n.endswith('.tmp')]
            self.assertEqual(leftovers, [])

    def test_same_process_concurrent_atomic_writes_do_not_share_a_temp(self):
        with _TempDir() as root:
            path = os.path.join(root, 'concurrent.json')
            values = ['{"writer": %d}\n' % index for index in range(8)]
            barrier = threading.Barrier(len(values))
            errors = []

            def write(value):
                try:
                    barrier.wait()
                    install_mod._atomic_write(path, value)
                except Exception as e:
                    errors.append(e)

            workers = [threading.Thread(target=write, args=(value,))
                       for value in values]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
            self.assertEqual(errors, [])
            with open(path, encoding='utf-8') as f:
                self.assertIn(f.read(), values)
            self.assertEqual([name for name in os.listdir(root)
                              if name.endswith('.tmp')], [])


# -- 10. THE uninstall preview -----------------------------------------

class TestConvoyInstallUninstallPlan(EmbodyTestCase):

    def _populated(self, root, jobs=3, indeterminate=1):
        install_mod.write_payload(root, '6.0.171', _MODULES)
        install_mod.write_installed(root, {'version': '6.0.171'})
        install_mod._atomic_write(install_mod.launcher_path(root), '#\n')
        for name in ('host.json', 'host.token', 'audit.jsonl'):
            with open(os.path.join(root, name), 'w') as f:
                f.write('{}')
        jobs_dir = os.path.join(root, 'jobs')
        os.makedirs(jobs_dir, exist_ok=True)
        for i in range(jobs):
            state = 'indeterminate' if i < indeterminate else 'succeeded'
            with open(os.path.join(jobs_dir, 'cj_%d.json' % i), 'w') as f:
                json.dump({'delivery_id': 'cj_%d' % i, 'state': state}, f)
        # Bookkeeping files HostStore.jobs() skips -- they are not
        # deliveries and must not be counted as jobs.
        with open(os.path.join(jobs_dir, 'idem_abc.json'), 'w') as f:
            f.write('{}')
        with open(os.path.join(jobs_dir, '_notes.json'), 'w') as f:
            f.write('{}')
        return root

    def test_host_json_host_token_and_jobs_are_RETAINED_never_removed(self):
        """THE NAMED TEST. 16.4/A-15 make indeterminate job records
        PERMANENT BY DESIGN and A-41 forbids uninstall as an
        evidence-destruction path -- so deleting these here would
        destroy exactly the evidence the design promises to keep.
        Deleting host state is a SEPARATE second action with its own
        confirmation, and a re-install after it mints a NEW host_id."""
        with _TempDir() as root:
            self._populated(root)
            plan = install_mod.plan_host_uninstall(root)
            removed = ' | '.join(plan['remove'] + plan['remove_dirs'])
            for name in ('host.json', 'host.token', 'audit.jsonl'):
                self.assertTrue(
                    any(p.endswith(name) for p in plan['retain']),
                    '%s must be named in retain' % name)
                self.assertNotIn(name, removed,
                                 '%s must NEVER appear in remove' % name)
            self.assertTrue(any(p.endswith('jobs') for p in plan['retain']))
            self.assertNotIn(os.path.join(root, 'jobs'), removed)
            # Every individual job record, too.
            self.assertNotIn('cj_0.json', removed)

    def test_it_counts_the_jobs_and_the_indeterminate_ones(self):
        """The confirmation NAMES the retained path and COUNTS what is
        retained -- 'N jobs, M indeterminate' -- so the user knows in
        numbers what is being kept."""
        with _TempDir() as root:
            self._populated(root, jobs=5, indeterminate=2)
            plan = install_mod.plan_host_uninstall(root)
            self.assertEqual(plan['jobs'], 5)
            self.assertEqual(plan['indeterminate'], 2)

    def test_idempotency_markers_are_not_counted_as_jobs(self):
        with _TempDir() as root:
            self._populated(root, jobs=0, indeterminate=0)
            self.assertEqual(install_mod.count_jobs(root), (0, 0))

    def test_counting_an_absent_or_unreadable_jobs_dir_is_zero(self):
        with _TempDir() as root:
            self.assertEqual(install_mod.count_jobs(root), (0, 0))
            jobs_dir = os.path.join(root, 'jobs')
            os.makedirs(jobs_dir)
            with open(os.path.join(jobs_dir, 'cj_x.json'), 'w') as f:
                f.write('{not json')
            total, indeterminate = install_mod.count_jobs(root)
            self.assertEqual((total, indeterminate), (1, 0))

    def test_the_payload_launcher_and_record_ARE_removed(self):
        with _TempDir() as root:
            self._populated(root)
            plan = install_mod.plan_host_uninstall(root)
            removed = ' | '.join(plan['remove'])
            for name in ('convoy_host_launch.py', 'installed.json',
                         'convoy_hostapp.py', '.complete'):
                self.assertIn(name, removed)

    def test_retain_is_canonical_not_filtered_by_existence(self):
        """A fresh install that has never run a job still promises to
        keep host.json. A preview that dropped it because the file is
        not there yet would be promising less than the design does."""
        with _TempDir() as root:
            plan = install_mod.plan_host_uninstall(root)
            self.assertTrue(any(p.endswith('host.json')
                                for p in plan['retain']))
            self.assertEqual(plan['retain_present'], [])

    def test_a_STRAY_DIRECTORY_under_app_is_reported_never_fatal(self):
        """REGRESSION GUARD, MEASURED 2026-08-01. installed_versions
        returned EVERY directory name; plan_host_uninstall handed each
        to app_dir -> safe_version, which RAISED. So a directory called
        'tmp junk' made the uninstall PREVIEW -- the dialog whose entire
        job is to name what is retained and count the indeterminate
        records -- raise ValueError out of a worker thread with no
        retain list at all.

        Report, do not destroy, do not explode."""
        with _TempDir() as root:
            self._populated(root)
            base = install_mod.app_dir(root, None)
            for junk in ('tmp junk', 'not@ok', '.hidden'):
                os.makedirs(os.path.join(base, junk), exist_ok=True)
            plan = install_mod.plan_host_uninstall(root)      # must not raise
            self.assertEqual(
                sorted(os.path.basename(s) for s in plan['stray']),
                ['.hidden', 'not@ok', 'tmp junk'])
            removed = ' | '.join(plan['remove'] + plan['remove_dirs'])
            for junk in ('tmp junk', 'not@ok', '.hidden'):
                self.assertNotIn(junk, removed,
                                 'a stray directory is not ours to delete')
            # ...and the real version is still planned for removal.
            self.assertIn('convoy_hostapp.py', removed)

    def test_an_INTERRUPTED_payload_is_not_promised_and_is_reported(self):
        """Files with no .complete: we cannot prove we wrote them, so we
        do not delete them -- but the preview used to list a phantom
        .complete under 'remove' and say nothing about the sources it
        was silently leaving on disk."""
        with _TempDir() as root:
            self._populated(root)
            target = install_mod.app_dir(root, '6.0.180')
            os.makedirs(target, exist_ok=True)
            orphan = os.path.join(target, 'convoy_hostapp.py')
            with open(orphan, 'w') as f:
                f.write('# interrupted\n')
            plan = install_mod.plan_host_uninstall(root)
            self.assertIn(orphan, plan['incomplete'])
            self.assertNotIn(orphan, plan['remove'])
            self.assertNotIn(os.path.join(target, '.complete'),
                             plan['remove'])

    def test_the_pycache_deletion_is_STATED_in_the_preview(self):
        """It is ours to remove, but the dialog has to say so -- a
        deletion the preview does not name is a silent one."""
        with _TempDir() as root:
            self._populated(root)
            cache = os.path.join(install_mod.app_dir(root, '6.0.171'),
                                 '__pycache__')
            os.makedirs(cache)
            with open(os.path.join(cache, 'convoy_hostapp.pyc'), 'wb') as f:
                f.write(b'\x00')
            plan = install_mod.plan_host_uninstall(root)
            self.assertTrue(any('__pycache__' in p for p in plan['remove']))

    def test_the_launch_agent_plist_is_removed_on_darwin(self):
        with _TempDir() as root:
            plan = install_mod.plan_host_uninstall(root, platform='darwin',
                                                   home='/Users/x')
            self.assertIn(
                '/Users/x/Library/LaunchAgents/'
                'tools.embody.convoy.host.plist', plan['remove'])


# -- 11. host_state: the ONE status computation ------------------------

class TestConvoyInstallHostState(EmbodyTestCase):

    RECORD = {'version': '6.0.171', 'supervisor': 'scheduled_task',
              'interpreter': '/py'}
    REGISTERED = {'registered': True, 'enabled': True, 'state': 'ready'}

    def test_nothing_installed(self):
        got = install_mod.host_state(None, 'absent')
        self.assertEqual(got['state'], 'not_installed')

    def test_running(self):
        got = install_mod.host_state(self.RECORD, 'running',
                                     self.REGISTERED, '6.0.171', True,
                                     pid=4242)
        self.assertEqual(got['state'], 'running')
        self.assertEqual(got['pid'], 4242)
        self.assertTrue(got['live'])

    def test_installed_but_not_answering(self):
        got = install_mod.host_state(self.RECORD, 'absent',
                                     self.REGISTERED, '6.0.171', True)
        self.assertEqual(got['state'], 'not_running')
        self.assertIn('within a minute', got['detail'])

    def test_installed_but_the_supervisor_is_disabled(self):
        got = install_mod.host_state(
            self.RECORD, 'absent',
            {'registered': True, 'enabled': False, 'state': 'disabled'},
            '6.0.171', True)
        self.assertEqual(got['state'], 'stopped')

    def test_installed_with_no_supervisor_at_all(self):
        got = install_mod.host_state(self.RECORD, 'absent',
                                     {'registered': False}, '6.0.171', True)
        self.assertEqual(got['state'], 'no_supervisor')
        got = install_mod.host_state(self.RECORD, 'absent', None,
                                     '6.0.171', True)
        self.assertEqual(got['state'], 'no_supervisor')

    def test_a_missing_interpreter_outranks_even_running(self):
        """A TD upgrade deletes the recorded interpreter while the
        daemon it launched keeps running: 'Running' would be true today
        and a permanent silent death tomorrow, with no warning in
        between. The actionable answer wins -- and `live` still rides
        along so a caller can say both."""
        got = install_mod.host_state(self.RECORD, 'running',
                                     self.REGISTERED, '6.0.171',
                                     interpreter_exists=False, pid=99)
        self.assertEqual(got['state'], 'needs_repair_python')
        self.assertTrue(got['live'])
        self.assertEqual(got['pid'], 99)
        self.assertIn('TouchDesigner upgrade', got['detail'])

    def test_a_newer_embody_installed_it(self):
        got = install_mod.host_state({'version': '6.0.180',
                                      'supervisor': 'scheduled_task'},
                                     'running', self.REGISTERED, '6.0.171',
                                     True)
        self.assertEqual(got['state'], 'newer_install')
        self.assertIn('6.0.180', got['detail'])

    def test_an_external_supervisor(self):
        got = install_mod.host_state({'version': '6.0.171',
                                      'supervisor': 'external'},
                                     'absent', None, '6.0.171', True)
        self.assertEqual(got['state'], 'external_supervisor')

    def test_skipping_the_interpreter_check_is_allowed(self):
        got = install_mod.host_state(self.RECORD, 'running',
                                     self.REGISTERED, '6.0.171',
                                     interpreter_exists=None)
        self.assertEqual(got['state'], 'running')

    def test_it_is_total_and_never_raises(self):
        for installed in (None, {}, 'x', 42, []):
            for probe in (None, 'absent', 'running', 'stale', 'weird'):
                for supervisor in (None, {}, 'x', {'registered': True}):
                    got = install_mod.host_state(installed, probe,
                                                 supervisor, '6.0.171')
                    self.assertIn('state', got)


# -- 12. install / start / stop / uninstall, with an injected runner ---

class TestConvoyInstallActions(EmbodyTestCase):

    def _install(self, *args, **kwargs):
        kwargs.setdefault('runtime_verifier', _approved_runtime)
        return install_mod.install(*args, **kwargs)

    def test_install_writes_everything_and_registers_once(self):
        with _TempDir() as root:
            runner = _Runner()
            got = self._install(root, '6.0.171', _MODULES, WIN_PY,
                                      platform='win32', runner=runner,
                                      env=WIN_ENV)
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['registered'])
            self.assertTrue(os.path.isfile(
                install_mod.launcher_path(root, 'win32')))
            self.assertTrue(os.path.isfile(
                install_mod.task_xml_path(root, 'win32')))
            self.assertTrue(os.path.isdir(
                install_mod.logs_dir(root, 'win32')))
            self.assertEqual(
                install_mod.read_installed(root, 'win32')['version'],
                '6.0.171')
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][:2], ['schtasks', '/Create'])

    def test_a_venv_runtime_records_no_managed_receipt(self):
        """The host app is plain Python (like the Envoy bridge) and may run
        under Embody's own uv venv interpreter. That shape has NO bundle to
        hash, so it records venv_runtime=True and NO 'runtime' receipt -- the
        two shapes are mutually exclusive, so a record can never claim managed
        verification it did not get."""
        def venv_verifier(data_dir, interpreter, platform=None,
                          architecture=None, runner=None):
            # What probe_runtime returns: a live crypto-floor pass, with no
            # runtime_id because there is no managed bundle.
            return {'ok': True, 'probe': {'python': [3, 11, 15],
                                          'cryptography_version': '49.0.0'}}

        with _TempDir() as root:
            got = install_mod.install(
                root, '6.0.171', _MODULES, WIN_PY, platform='win32',
                runner=_Runner(), env=WIN_ENV,
                runtime_verifier=venv_verifier)
            self.assertTrue(got['ok'], got)
            record = install_mod.read_installed(root, 'win32')
            self.assertTrue(record.get('venv_runtime'))
            self.assertNotIn(
                'runtime', record,
                'a venv install must not fabricate a managed-runtime receipt')
            self.assertEqual(record['interpreter'], WIN_PY)

    def test_a_managed_runtime_records_a_receipt_and_no_venv_flag(self):
        with _TempDir() as root:
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                          platform='win32', runner=_Runner(), env=WIN_ENV)
            record = install_mod.read_installed(root, 'win32')
            self.assertEqual(record['runtime']['format'],
                             install_mod.RUNTIME_RECEIPT_FORMAT)
            self.assertFalse(record.get('venv_runtime'),
                             'a managed install is never marked venv')

    def test_the_launcher_venv_branch_still_pins_the_interpreter(self):
        """The venv branch relaxes the BUNDLE checks (there is no bundle) but
        must still refuse an interpreter other than the one recorded at
        install -- otherwise a rewritten installed.json could point the login
        task at any Python."""
        source = install_mod.render_launcher('win32', 'C:/tmp/fake')
        compile(source, 'convoy_host_launch.py', 'exec')
        self.assertIn('venv_runtime', source)
        self.assertIn('recorded at install', source)
        # The live crypto/TLS proof replaces the bundle-integrity proof.
        self.assertIn('HAS_TLSv1_3', source)
        self.assertIn('cryptography_available', source)

    def test_installed_json_is_written_LAST_of_all(self):
        """It is what the launcher reads to find its payload. Written
        earlier, a crash mid-install would leave a record pointing at
        files that do not exist; written last, a crash leaves the
        PREVIOUS install intact and running."""
        with _TempDir() as root:
            runner = _Runner()
            got = self._install(root, '6.0.171', _MODULES, WIN_PY,
                                      platform='win32', runner=runner,
                                      env=WIN_ENV)
            self.assertEqual(got['steps'][-1], 'installed.json')
            self.assertEqual(got['steps'][0], 'payload')

    def test_a_failed_registration_leaves_no_install_record(self):
        """Half-installed must never read as installed."""
        with _TempDir() as root:
            runner = _Runner(returncode=1, stderr='ERROR: access denied')
            got = self._install(root, '6.0.171', _MODULES, WIN_PY,
                                      platform='win32', runner=runner,
                                      env=WIN_ENV)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'register_failed')
            self.assertIn('access denied', got['detail'])
            self.assertIsNone(install_mod.read_installed(root, 'win32'))

    def test_install_registers_the_task_for_THIS_account(self):
        """The XML actually written to disk must carry the UserId, in
        both places -- the defect measured 2026-08-01 was invisible
        until a real schtasks saw the file."""
        with _TempDir() as root:
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner(),
                                user=WIN_USER)
            with open(install_mod.task_xml_path(root, 'win32'), 'rb') as f:
                text = f.read()[2:].decode('utf-16-le')
            self.assertEqual(
                re.findall(r'<UserId>([^<]*)</UserId>', text),
                [WIN_USER, WIN_USER])
            self.assertEqual(
                install_mod.read_installed(root, 'win32')['account'],
                WIN_USER)

    def test_install_REFUSES_when_it_cannot_name_the_account(self):
        """Rather than registering a task for 'any user', which is an
        administrator-only registration that schtasks denies."""
        with _TempDir() as root:
            runner = _Runner()
            # env injected empty: without the seam this would fall
            # through to the real environment, which always has a user.
            got = self._install(
                root, '6.0.171', _MODULES, WIN_PY, platform='win32',
                runner=runner, user=None, env={})
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'no_user_account')
            self.assertEqual(runner.calls, [],
                             'nothing may be registered without an account')
            self.assertIsNone(install_mod.read_installed(root, 'win32'))

    def test_a_darwin_install_needs_no_account(self):
        """A LaunchAgent is per-user by construction -- an account
        requirement there would be a Windows habit leaking across."""
        with _TempDir() as root:
            got = self._install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin',
                                      runner=_launchctl_runner(),
                                      home=os.path.join(root, 'home'),
                                      user=None)
            self.assertTrue(got['ok'], got)

    def test_install_records_the_interpreter_and_the_drain_interval(self):
        with _TempDir() as root:
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner(),
                                drain_interval=5.0,
                                installed_by='/project/x.toe', env=WIN_ENV)
            record = install_mod.read_installed(root, 'win32')
            self.assertEqual(record['interpreter'], WIN_PY)
            self.assertEqual(record['drain_interval'], 5.0)
            self.assertEqual(record['installed_by'], '/project/x.toe')
            self.assertEqual(record['supervisor'], 'scheduled_task')
            self.assertEqual(record['runtime']['format'],
                             install_mod.RUNTIME_RECEIPT_FORMAT)
            self.assertEqual(record['runtime']['architecture'], 'x86_64')
            self.assertEqual(record['runtime']['cryptography_version'],
                             'test')

    def test_an_external_supervisor_install_registers_NOTHING(self):
        """A-36: write the payload, never a second supervisor."""
        with _TempDir() as root:
            runner = _Runner()
            got = self._install(
                root, '6.0.171', _MODULES, WIN_PY, platform='win32',
                runner=runner, supervisor=install_mod.SUPERVISOR_EXTERNAL)
            self.assertTrue(got['ok'])
            self.assertFalse(got['registered'])
            self.assertEqual(runner.calls, [],
                             'an external supervisor must not be replaced')
            self.assertEqual(
                install_mod.read_installed(root, 'win32')['supervisor'],
                'external')

    def test_install_never_raises(self):
        with _TempDir() as root:
            for bad in (('../evil', _MODULES, WIN_PY),
                        ('6.0.171', None, WIN_PY),
                        ('6.0.171', _MODULES, None),
                        ('6.0.171', {}, WIN_PY)):
                got = self._install(root, bad[0], bad[1], bad[2],
                                    platform='win32', runner=_Runner())
                self.assertFalse(got['ok'])
                self.assertIn('reason', got)

    def test_a_darwin_install_ENABLES_before_it_bootstraps(self):
        """launchctl's disabled state is PERSISTENT, lives OUTSIDE the
        plist, is keyed by the constant label, and survives boots. Both
        stop() and uninstall() disable (they must -- KeepAlive would
        resurrect the agent in about a second), so an install that only
        bootstrapped left the plan's designated repair path
        (Stop -> Install, Uninstall -> Install) permanently unloadable.

        The Windows twin is safe only by accident of mechanism --
        schtasks /Create /F rewrites <Enabled>true</Enabled> -- so this
        asymmetry is invisible on the one platform we can test on
        hardware. It is pure argv decision logic, which is why it is
        assertable here without a Mac."""
        with _TempDir() as root:
            home = os.path.join(root, 'home')
            runner = _launchctl_runner()
            got = self._install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=runner,
                                      home=home, uid=501)
            self.assertTrue(got['ok'], got)
            self.assertTrue(os.path.isfile(
                install_mod.plist_path(home, 'darwin')))
            # The leading `print` is the loaded-label probe; on a fresh
            # install the label is absent so no bootout runs.
            self.assertEqual([c[1] for c in runner.calls],
                             ['print', 'enable', 'bootstrap'])
            self.assertIn('gui/501/tools.embody.convoy.host',
                          runner.calls[0])

    def test_a_darwin_repair_over_a_loaded_agent_boots_it_out_first(self):
        """The field failure (macOS 26, 2026-08-04): Repair Convoy App IS a
        full install, and bootstrapping a still-loaded label fails with
        EIO(5) -- 'Bootstrap failed: 5: Input/output error'. A loaded
        label is now disabled (KeepAlive would resurrect the old daemon),
        booted out, and WAITED for (bootout returns before launchd drops
        the label) before the enable/bootstrap pair runs."""
        with _TempDir() as root:
            home = os.path.join(root, 'home')
            runner = _launchctl_runner(loaded=True)
            got = self._install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=runner,
                                      home=home, uid=501)
            self.assertTrue(got['ok'], got)
            self.assertEqual(
                [c[1] for c in runner.calls],
                ['print', 'disable', 'bootout', 'print',
                 'enable', 'bootstrap'])
            self.assertIn('bootout', got['steps'])
            self.assertIsNotNone(install_mod.read_installed(root, 'darwin'))

    def test_a_darwin_repair_asks_the_running_daemon_to_exit_first(self):
        """With graceful observers supplied (the ConvoyExt worker passes
        the same shutdown/is_running pair stop() uses), a repair over a
        RUNNING daemon asks it to exit and waits before touching launchd
        -- bootout alone would be a hard kill mid-job."""
        with _TempDir() as root:
            home = os.path.join(root, 'home')
            runner = _launchctl_runner(loaded=True)
            order = []
            alive = {'running': True}

            def shutdown():
                order.append('shutdown')
                alive['running'] = False

            def is_running():
                return alive['running']

            original = runner.__call__

            def recording(argv, timeout_s=None):
                order.append(argv[1] if argv[0] == 'launchctl' else argv[0])
                return original(argv, timeout_s=timeout_s)

            got = self._install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=recording,
                                      home=home, uid=501,
                                      shutdown=shutdown,
                                      is_running=is_running)
            self.assertTrue(got['ok'], got)
            self.assertIn('stopped_for_repair', got['steps'])
            self.assertEqual(order[0], 'shutdown',
                             'the graceful exit precedes every launchctl call')

    def test_a_darwin_bootstrap_that_still_fails_is_a_real_failure(self):
        """With the label genuinely gone, a failing bootstrap is a real
        register_failed -- the stderr reaches the caller verbatim."""
        with _TempDir() as root:
            home = os.path.join(root, 'home')

            def rc(argv):
                if argv[1] == 'print':
                    return 5           # not loaded
                if argv[1] == 'bootstrap':
                    return 5
                return 0

            runner = _Runner(returncode=rc,
                             stderr='Bootstrap failed: 5: Input/output '
                                    'error')
            got = self._install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=runner,
                                      home=home, uid=501)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'register_failed')
            self.assertIn('Input/output error', got['detail'])
            self.assertIsNone(install_mod.read_installed(root, 'darwin'))

    def test_a_windows_repair_asks_the_running_daemon_to_exit_first(self):
        """schtasks /Create /F rewrites the DEFINITION of a running task,
        but the old process keeps running the old code -- the same silent
        staleness, without an error to notice. The graceful exit applies
        on win32 too."""
        with _TempDir() as root:
            called = []
            alive = {'running': True}

            def shutdown():
                called.append('shutdown')
                alive['running'] = False

            runner = _Runner()
            original = runner.__call__

            def recording(argv, timeout_s=None):
                called.append(argv[0])
                return original(argv, timeout_s=timeout_s)

            got = self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=recording,
                                user=WIN_USER,
                                shutdown=shutdown,
                                is_running=lambda: alive['running'])
            self.assertTrue(got['ok'], got)
            self.assertEqual(called[0], 'shutdown',
                             'the graceful exit precedes the re-register')
            self.assertIn('schtasks', called)
            self.assertIn('stopped_for_repair', got['steps'])

    def test_a_label_lingering_after_bootout_is_waited_for(self):
        """bootout returns before launchd drops the label -- the settle
        loop exists for exactly that window. Model it: `print` keeps
        answering loaded for two polls after bootout, then the label is
        gone and bootstrap proceeds."""
        with _TempDir() as root:
            home = os.path.join(root, 'home')
            state = {'loaded': True, 'linger': 2}

            def rc(argv):
                if argv[0] != 'launchctl':
                    return 0
                if argv[1] == 'print':
                    if not state['loaded']:
                        return 5
                    if state['linger'] <= 0:
                        state['loaded'] = False
                        return 5
                    return 0
                if argv[1] == 'bootout':
                    state['linger'] = 2   # teardown is asynchronous
                return 0

            runner = _Runner(returncode=rc)
            real_call = runner.__call__

            def counting(argv, timeout_s=None):
                if argv[0] == 'launchctl' and argv[1] == 'print' \
                        and state['loaded']:
                    state['linger'] -= 1
                return real_call(argv, timeout_s=timeout_s)

            got = self._install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=counting,
                                      home=home, uid=501,
                                      sleep=lambda s: None)
            self.assertTrue(got['ok'], got)
            prints = [c for c in runner.calls
                      if c[0] == 'launchctl' and c[1] == 'print']
            self.assertGreater(len(prints), 2,
                               'the settle loop polled past the linger')
            self.assertEqual(runner.calls[-1][1], 'bootstrap')

    def test_await_unregistered_is_bounded_by_fake_time(self):
        """A label that never unloads must not hang the install: the
        settle poll gives up at its bound, on injected time."""
        clock = {'t': 0.0}
        sleeps = []

        def now():
            return clock['t']

        def sleep(s):
            sleeps.append(s)
            clock['t'] += s

        stuck = _Runner(returncode=0)   # `print` always says loaded
        gone = install_mod._await_unregistered(
            stuck, 'darwin', uid=501, timeout_s=1.0,
            sleep=sleep, now=now)
        self.assertFalse(gone)
        self.assertTrue(sleeps, 'it polled before giving up')

    def test_a_windows_install_needs_no_enable_step(self):
        """schtasks /Create /F rewrites the whole definition including
        <Enabled>true</Enabled>, so registration already re-enables."""
        with _TempDir() as root:
            runner = _Runner()
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=runner,
                                user=WIN_USER)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][:2], ['schtasks', '/Create'])

    def test_a_darwin_install_without_a_home_is_refused(self):
        with _TempDir() as root:
            got = self._install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=_Runner())
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'no_home')

    def test_stop_DISABLES_before_it_stops(self):
        """Or the daemon is back within 60 s on Windows (repetition
        trigger) or about 1 s on macOS (KeepAlive), and the Stop button
        looks broken."""
        for platform, second in (('win32', '/End'), ('darwin', 'bootout')):
            runner = _Runner()
            got = install_mod.stop(platform=platform, runner=runner,
                                   uid=501)
            self.assertTrue(got['ok'])
            self.assertEqual(len(runner.calls), 2)
            self.assertIn('disable', ' '.join(runner.calls[0]).lower())
            self.assertIn(second, runner.calls[1])

    def test_stop_asks_the_daemon_to_exit_FIRST(self):
        """The authenticated POST /shutdown runs before the supervisor
        stop, so the daemon clears its own portfile. A hard stop first
        would leave one naming a dead port."""
        order = []
        runner = _Runner()

        def note_calls(argv, timeout_s=None):
            order.append('supervisor')
            return runner(argv, timeout_s)

        def shutdown():
            order.append('shutdown')
            return {'ok': True}

        got = install_mod.stop(platform='win32', runner=note_calls,
                               shutdown=shutdown)
        self.assertTrue(got['ok'])
        self.assertEqual(order[0], 'shutdown')
        self.assertEqual(got['shutdown'], {'ok': True})

    def test_stop_WAITS_for_the_daemon_to_actually_exit(self):
        """REGRESSION GUARD, MEASURED 2026-08-01. /shutdown answers
        {"stopping": true} -- not "stopped" -- and the daemon then needs
        ~0.52 s to unwind main()'s finally and clear its portfile (up to
        ~65 s with a forward in flight). Two schtasks spawns take 57 ms,
        so WITHOUT a wait the supervisor stop landed ~460 ms into a
        ~520 ms unwind on EVERY ordinary stop: exactly the hard kill
        leaving a stale portfile that POST /shutdown exists to avoid.

        The old test asserted call ORDER only, so the suite stayed green
        over it."""
        events = []
        alive = [True]

        def shutdown():
            events.append('shutdown')
            return {'ok': True}

        def is_running():
            # Alive for the first two polls, then gone.
            events.append('poll')
            if events.count('poll') >= 3:
                alive[0] = False
            return alive[0]

        def runner(argv, timeout_s=None):
            events.append('supervisor')
            return 0, '', ''

        got = install_mod.stop(platform='win32', runner=runner,
                               shutdown=shutdown, is_running=is_running,
                               sleep=lambda s: events.append('sleep'))
        self.assertTrue(got['ok'])
        self.assertIs(got['exited'], True)
        # Every poll happens before the first supervisor call.
        self.assertLess(events.index('poll'), events.index('supervisor'))
        self.assertEqual(events[0], 'shutdown')

    def test_stop_is_BOUNDED_and_stops_the_supervisor_anyway(self):
        """A daemon that never exits must not hang the UI forever. Past
        the bound we proceed to the supervisor stop -- that IS the
        backstop -- and report exited:False rather than pretending."""
        slept = []
        got = install_mod.stop(platform='win32', runner=_Runner(),
                               shutdown=lambda: {'ok': True},
                               is_running=lambda: True,
                               exit_timeout_s=0.3,
                               sleep=lambda s: slept.append(s))
        self.assertTrue(got['ok'])
        self.assertIs(got['exited'], False)
        self.assertTrue(slept, 'it must actually have waited')

    def test_without_an_observer_stop_says_so_rather_than_guessing(self):
        got = install_mod.stop(platform='win32', runner=_Runner(),
                               shutdown=lambda: {'ok': True})
        self.assertIsNone(got['exited'])

    def test_uninstall_waits_before_unlinking_the_running_payload(self):
        """More load-bearing here than in stop(): uninstall unlinks the
        payload modules and installed.json out from under a daemon that
        may still be running."""
        with _TempDir() as root:
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner(),
                                user=WIN_USER)
            events = []

            def is_running():
                events.append('poll')
                return len(events) < 2

            got = install_mod.uninstall(
                root, platform='win32', runner=_Runner(),
                shutdown=lambda: events.append('shutdown') or {'ok': True},
                is_running=is_running, sleep=lambda s: None)
            self.assertTrue(got['ok'], got)
            self.assertIs(got['exited'], True)
            self.assertEqual(events[0], 'shutdown')

    def test_a_daemon_that_will_not_answer_still_gets_stopped(self):
        """A wedged daemon is exactly why the supervisor stop exists."""
        runner = _Runner()

        def shutdown():
            raise OSError('connection refused')

        got = install_mod.stop(platform='win32', runner=runner,
                               shutdown=shutdown)
        self.assertTrue(got['ok'])
        self.assertFalse(got['shutdown']['ok'])
        self.assertEqual(len(runner.calls), 2)

    def test_start_ENABLES_before_it_starts(self):
        """/Run on a disabled task silently does nothing on Windows, and
        Stop is what disabled it."""
        runner = _Runner()
        got = install_mod.start(platform='win32', runner=runner)
        self.assertTrue(got['ok'])
        self.assertIn('/ENABLE', runner.calls[0])
        self.assertIn('/Run', runner.calls[1])

    def test_start_on_darwin_bootstraps_before_kickstarting(self):
        """stop() on darwin is `bootout`, which removes the job from the
        domain entirely -- kickstart would have nothing to kick."""
        runner = _Runner()
        got = install_mod.start(platform='darwin', runner=runner,
                                uid=501, home='/Users/x')
        self.assertTrue(got['ok'])
        actions = [c[1] for c in runner.calls]
        self.assertEqual(actions, ['enable', 'bootstrap', 'kickstart'])

    def test_start_reports_a_real_failure(self):
        runner = _Runner(returncode=1, stderr='ERROR: cannot start')
        got = install_mod.start(platform='win32', runner=runner)
        self.assertFalse(got['ok'])
        self.assertEqual(got['reason'], 'start_failed')

    def test_uninstall_removes_the_payload_and_keeps_the_evidence(self):
        with _TempDir() as root:
            runner = _Runner()
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=runner)
            for name in ('host.json', 'host.token'):
                with open(os.path.join(root, name), 'w') as f:
                    f.write('keep me')
            jobs_dir = os.path.join(root, 'jobs')
            os.makedirs(jobs_dir, exist_ok=True)
            with open(os.path.join(jobs_dir, 'cj_1.json'), 'w') as f:
                json.dump({'state': 'indeterminate'}, f)

            got = install_mod.uninstall(root, platform='win32',
                                        runner=_Runner())
            self.assertTrue(got['ok'], got)
            self.assertFalse(os.path.exists(
                install_mod.launcher_path(root, 'win32')))
            self.assertFalse(os.path.exists(
                install_mod.installed_path(root, 'win32')))
            self.assertFalse(os.path.exists(
                install_mod.app_dir(root, None, 'win32')))
            # THE EVIDENCE SURVIVES.
            for name in ('host.json', 'host.token'):
                self.assertTrue(os.path.isfile(os.path.join(root, name)),
                                '%s must survive an uninstall' % name)
            self.assertTrue(os.path.isfile(
                os.path.join(jobs_dir, 'cj_1.json')),
                'an indeterminate job record is permanent by design')

    def test_uninstall_unregisters_the_supervisor(self):
        with _TempDir() as root:
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner(),
                                env=WIN_ENV)
            runner = _Runner()
            install_mod.uninstall(root, platform='win32', runner=runner)
            joined = [' '.join(c) for c in runner.calls]
            self.assertTrue(any('/DISABLE' in c for c in joined))
            self.assertTrue(any('/Delete' in c for c in joined))

    def test_uninstall_never_raises(self):
        with _TempDir() as root:
            got = install_mod.uninstall(root, platform='win32',
                                        runner=_Runner())
            self.assertIn('ok', got)

    def test_uninstall_keeps_a_stranger_and_reports_it(self):
        with _TempDir() as root:
            self._install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner(),
                                env=WIN_ENV)
            stranger = os.path.join(
                install_mod.app_dir(root, '6.0.171', 'win32'), 'mine.txt')
            with open(stranger, 'w') as f:
                f.write('mine')
            install_mod.uninstall(root, platform='win32', runner=_Runner())
            self.assertTrue(os.path.isfile(stranger))


# -- 13. the module's own constraints ----------------------------------

class TestConvoyInstallIsTouchDesignerFree(EmbodyTestCase):

    def test_it_imports_nothing_from_touchdesigner(self):
        """It runs on a worker thread and under plain pytest with no TD
        present. A td import would break both.

        AST again: a substring scan for 'op(' also matches 'stop(' and
        'Popen(', which is how this kind of test ends up either
        permanently red or quietly weakened until it proves nothing."""
        tree = _module_ast()
        for module in _imported_modules(tree):
            self.assertNotIn(module.split('.')[0],
                             ('td', 'TDFunctions', 'TDStoreTools',
                              'touchdesigner'),
                             '%s is a TouchDesigner import' % module)
        # The TD globals are never referenced either -- they do not exist
        # off the main thread, let alone under pytest. Checked against
        # FREE names only (loaded but never bound anywhere in the
        # module): `run = runner or run_command` binds a local called
        # `run`, which is not TouchDesigner's `run()` and must not fail
        # this test.
        free = _free_names(tree)
        for td_global in TD_GLOBALS:
            self.assertNotIn(td_global, free,
                             '%r resolves to a TouchDesigner global'
                             % td_global)

    def test_the_free_name_check_would_actually_catch_a_td_global(self):
        """A GUARD ON THE GUARD, and it has already earned its keep.

        _free_names is only doing work if it flags a genuine unbound TD
        reference. The first version subtracted a live dir(builtins) --
        and BOTH RUNNERS PUT TD GLOBALS THERE (the pytest shim injects
        `op` and `project`; inside TD they really are builtins), so the
        check silently subtracted the very names it was searching for
        and passed on a file that referenced op() directly. Without this
        test that would have shipped as green."""
        tainted = ast.parse('def f():\n    return op("/x").par.X\n')
        self.assertIn('op', _free_names(tainted))
        self.assertIn('project', _free_names(
            ast.parse('x = project.folder\n')))
        clean = ast.parse('def f():\n    op = 1\n    return op\n')
        self.assertNotIn('op', _free_names(clean))
        # A real builtin must still be filtered out, or every module
        # would look like it referenced TD.
        self.assertNotIn('len', _free_names(ast.parse('x = len([])\n')))

    def test_every_import_is_stdlib(self):
        """No third-party dependency may creep in: this module has to
        import cleanly inside TD's interpreter and on a bare CI runner
        with nothing but pytest installed."""
        allowed = {'hashlib', 'json', 'ntpath', 'os', 'platform',
                   'posixpath', 're', 'stat', 'subprocess', 'sys', 'time',
                   'xml', 'zipfile'}
        for module in _imported_modules(_module_ast()):
            self.assertIn(module.split('.')[0], allowed,
                          '%s is not in the stdlib allowlist' % module)

    def test_it_is_ascii_only_with_clean_newlines_and_no_bom(self):
        """No BOM, ASCII only, no stray CR.

        This used to assert `b'\\r\\n' not in raw`. That became
        untestable the moment convoy_install.py had to become a text DAT
        so it would actually SHIP in the .tox (it was committed but
        vendored nowhere): Embody writes every externalized DAT with CRLF
        on Windows, so the assertion failed on every Windows dev machine
        while passing in CI -- the worst kind of test.

        The invariant it was protecting is intact and still enforced,
        just not by this line: .gitattributes declares `*.py text eol=lf`,
        so the COMMITTED bytes are LF regardless of the working tree.
        What is asserted here is what a working tree can actually
        promise -- no BOM, pure ASCII, and no LONE carriage return, which
        would be real corruption rather than a platform convention.
        """
        with open(_INSTALL_PATH, 'rb') as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'), 'no BOM')
        stripped = raw.replace(b'\r\n', b'\n')
        self.assertNotIn(b'\r', stripped,
                         'a CR not part of a CRLF pair is corruption')
        raw.decode('ascii')

    def test_HOST_MODULES_names_exactly_the_daemons_real_modules(self):
        """HOST_MODULES documents itself as 'the manifest of what to
        vendor'. This is what makes that claim true rather than
        decorative: it is checked against the actual daemon sources in
        dev/convoy/, so adding a tenth module (or renaming one) without
        updating the list fails here -- long before Step 3 vendors a set
        the launcher cannot import."""
        on_disk = set()
        for name in os.listdir(_CONVOY_DIR):
            if not name.endswith('.py') or name.startswith('test_'):
                continue
            # conftest is pytest's, manual_exit_proof is a hand-run
            # harness, and vendor_host_modules is the repo tool that
            # COPIES the daemon into the .tox -- none of the three is
            # imported by the daemon, so none belongs in the manifest of
            # what to vendor. (vendor_host_modules itself discovers by
            # globbing convoy_*.py, which is why it does not have this
            # problem in the other direction.)
            if name in ('conftest.py', 'manual_exit_proof.py',
                        'vendor_host_modules.py',
                        'build_convoy_runtime_bundle.py'):
                continue
            on_disk.add(name)
        self.assertEqual(set(install_mod.HOST_MODULES), on_disk)
        # The count is asserted as well as the set so that a rename plus
        # an addition cannot cancel out. It moved 9 -> 10 when Phase 3
        # slice 1 added convoy_hostkeys.py, 10 -> 11 when slice 2 added
        # convoy_peers.py, and 11 -> 14 when slice 3 added convoy_lan.py,
        # convoy_peerserver.py and convoy_peerclient.py (the LAN
        # transport), 14 -> 21 for discovery/realm, host-private policy,
        # artifacts/HTTP, wake, and host operations, 21 -> 23 for the optional
        # Owlette public-API consumer and host-native lifecycle manager, and
        # 23 -> 25 for full-duplex WebSocket sessions -- THIS TEST is what
        # catches a payload that would otherwise ship without an imported
        # module.
        self.assertEqual(len(install_mod.HOST_MODULES), 25,
                         'nine plan modules, convoy_hostkeys.py (slice 1), '
                         'convoy_peers.py (slice 2), and convoy_lan.py + '
                         'convoy_peerserver.py + convoy_peerclient.py '
                         '(slice 3 transport), plus seven completed '
                         'host-owned Convoy modules, Owlette, lifecycle, and '
                         'the WebSocket session transport')

    def test_the_module_docstring_states_the_honest_limits(self):
        """The code must not read softer than the install dialog. If
        these sentences go, the docs and the dialog have drifted from
        what is actually true."""
        doc = install_mod.__doc__
        for claim in ('per-user', 'UNVERIFIED', 'self-contained',
                      'signed', 'never downloads'):
            self.assertIn(claim, doc)
