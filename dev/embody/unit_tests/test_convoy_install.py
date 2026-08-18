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
import inspect
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


def _pe_bytes(subsystem, magic=0x010B, e_lfanew=0x80, size=None, tail=b''):
    """A PE image header carrying one honest fact: its subsystem.

    Enough of a real Portable Executable for pe_subsystem to read: the
    DOS stub's e_lfanew at 0x3C, the PE signature it points at, a COFF
    header, and an optional header whose magic says PE32 (0x010b) or
    PE32+ (0x020b). `tail` makes two otherwise identical fixtures
    distinguishable, so a test can prove WHICH file was copied.

    Crafted rather than copied from the machine: these tests run on the
    macOS CI leg too, where no Windows binary exists to borrow.
    """
    size = size or (e_lfanew + 96)
    raw = bytearray(size)
    raw[0:2] = b'MZ'
    raw[0x3C:0x40] = e_lfanew.to_bytes(4, 'little')
    raw[e_lfanew:e_lfanew + 4] = b'PE\0\0'
    raw[e_lfanew + 4:e_lfanew + 6] = (0x8664).to_bytes(2, 'little')
    raw[e_lfanew + 24:e_lfanew + 26] = magic.to_bytes(2, 'little')
    raw[e_lfanew + 92:e_lfanew + 94] = subsystem.to_bytes(2, 'little')
    return bytes(raw) + tail


def _gui_pe(tail=b''):
    return _pe_bytes(install_mod.PE_SUBSYSTEM_GUI, tail=tail)


def _console_pe(tail=b''):
    return _pe_bytes(install_mod.PE_SUBSYSTEM_CONSOLE, tail=tail)


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(payload)
    return path


def _read(path):
    with open(path, 'rb') as f:
        return f.read()


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


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
                 python_version='3.11.15', subsystem=None):
        target = os.path.join(root, runtime_id)
        relative = ('python/pythonw.exe' if platform == 'win32'
                    else 'python/bin/python3')
        probe_relative = ('python/python.exe' if platform == 'win32'
                          else relative)
        interpreter = os.path.join(target, *relative.split('/'))
        os.makedirs(os.path.dirname(interpreter), exist_ok=True)
        # A REAL PE HEADER on win32: 'windowless' is no longer a claim the
        # receipt makes, it is read out of the binary, so a fixture of
        # plain bytes would answer False and this fixture would stop
        # describing a runtime anyone would ship.
        with open(interpreter, 'wb') as f:
            f.write(_pe_bytes(subsystem or install_mod.PE_SUBSYSTEM_GUI,
                              tail=b'fake managed python')
                    if platform == 'win32' else b'fake managed python')
        probe_interpreter = os.path.join(target,
                                         *probe_relative.split('/'))
        if probe_interpreter != interpreter:
            with open(probe_interpreter, 'wb') as f:
                f.write(_console_pe(b'fake managed probe python'))
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

    def test_a_console_interpreter_is_never_windowless_and_loses(self):
        """'windowless' was hardcoded True -- a claim made by the file's
        NAME. It is read out of the binary now, so a runtime whose
        pythonw.exe is really a console build is passed over for one that
        is not, however much newer it is."""
        with _TempDir() as root:
            console = self._runtime(
                root, 'runtime-newer', 'win32', 'x86_64', '3.12.2',
                subsystem=install_mod.PE_SUBSYSTEM_CONSOLE)
            windowless = self._runtime(root, 'runtime-older', 'win32',
                                       'x86_64', '3.11.15')
            found = install_mod.find_interpreters(
                'win32', roots=[root], architecture='x86_64')
            self.assertEqual({c['path']: c['windowless'] for c in found},
                             {console: False, windowless: True})
            self.assertEqual(install_mod.choose_interpreter(found),
                             windowless,
                             'the newest runtime still loses to the one '
                             'that will not open a window at logon')

    def test_posix_runtimes_stay_windowless_with_no_pe_to_read(self):
        with _TempDir() as root:
            self._runtime(root, 'runtime-mac', 'darwin', 'arm64')
            found = install_mod.find_interpreters(
                'darwin', roots=[root], architecture='arm64')
            self.assertEqual([c['windowless'] for c in found], [True])

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


class TestConvoyDaemonVenvSpec(EmbodyTestCase):
    """Where the per-user daemon venv lives, and what may host it.

    This decision spent a release inside ConvoyExt._convoyDaemonVenvSpec,
    a TouchDesigner extension method reachable only by the TD-only suite
    that pytest skips -- so its Windows half had no test on any runner at
    all. Injected exists/isdir/listdir/env/base_prefix bring it here, to
    the windows+macos matrix, with LITERAL path strings per this file's
    convention (os.path.join would answer the darwin cases in backslashes
    on the Windows runner and prove nothing).
    """

    WIN_DATA = r'C:\Users\dev\AppData\Local\EmbodyConvoy'
    MAC_DATA = '/Users/dev/Library/Application Support/EmbodyConvoy'
    WIN_ENV = {'ProgramFiles': r'C:\Program Files',
               'LOCALAPPDATA': r'C:\Users\dev\AppData\Local',
               'APPDATA': r'C:\Users\dev\AppData\Roaming'}
    UV_DIR = r'C:\Users\dev\AppData\Roaming\uv\python'

    def _spec(self, platform, present=(), dirs=(), listing=None, env=None,
              base_prefix='', data_dir=None):
        # base_prefix defaults to '' (inert), NOT None. daemon_venv_spec
        # reads sys.base_prefix when it gets None, which silently drags the
        # RUNNING interpreter into a test whose whole premise is a faked
        # filesystem -- the TouchDesigner-base-python rung then offers it.
        # That is invisible until the runner's own base prefix happens to
        # collide with a path the test fakes: on this repo's dev box
        # sys.base_prefix IS 'C:\\Program Files\\Python39', so the
        # below-the-floor test failed under a 3.9 runner and passed under
        # 3.11+. '' is falsy, so the TD rung is skipped for every test that
        # does not care about it; the four that DO still pass one explicitly.
        present = set(present)
        dirs = set(dirs)
        listing = listing or {}
        if data_dir is None:
            data_dir = self.WIN_DATA if platform == 'win32' else self.MAC_DATA
        if env is None:
            env = self.WIN_ENV if platform == 'win32' else {}
        return install_mod.daemon_venv_spec(
            data_dir, platform=platform,
            exists=lambda p: p in present,
            isdir=lambda p: p in dirs,
            listdir=lambda p: list(listing.get(p, ())),
            env=env, base_prefix=base_prefix)

    # -- the interpreter pair -------------------------------------------

    def test_windows_splits_the_console_and_daemon_interpreters(self):
        """pythonw.exe is what the supervisor launches and what
        installed.json records; python.exe is what uv is driven with.
        Swapping them is invisible -- the launcher refuses to start
        unless the recorded interpreter realpath-matches the running one,
        and the Scheduled Task just retries every minute."""
        spec = self._spec('win32')
        self.assertEqual(
            spec['dir'],
            r'C:\Users\dev\AppData\Local\EmbodyConvoy\runtime-venv')
        self.assertEqual(
            spec['python'],
            r'C:\Users\dev\AppData\Local\EmbodyConvoy'
            r'\runtime-venv\Scripts\python.exe')
        self.assertEqual(
            spec['daemon_python'],
            r'C:\Users\dev\AppData\Local\EmbodyConvoy'
            r'\runtime-venv\Scripts\pythonw.exe')
        self.assertNotEqual(spec['python'], spec['daemon_python'])

    def test_macos_uses_one_interpreter_for_both(self):
        """posix has no windowless twin, so the two keys collapse -- and
        callers may read daemon_python unconditionally."""
        spec = self._spec('darwin')
        expected = ('/Users/dev/Library/Application Support/EmbodyConvoy'
                    '/runtime-venv/bin/python3')
        self.assertEqual(spec['python'], expected)
        self.assertEqual(spec['daemon_python'], expected)

    def test_a_platform_with_no_fallback_story_gets_no_spec(self):
        """Better no fallback than an invented one."""
        self.assertIsNone(self._spec('linux'))
        self.assertIsNone(self._spec('win32', data_dir=''))

    # -- which interpreters may host it ---------------------------------

    def test_windows_bases_are_python_org_then_uv_then_touchdesigner(self):
        spec = self._spec(
            'win32',
            present=[r'C:\Program Files\Python311\python.exe',
                     r'C:\Users\dev\AppData\Local\Programs\Python'
                     r'\Python312\python.exe',
                     self.UV_DIR + r'\cpython-3.13.2-windows-x86_64-none'
                                   r'\python.exe',
                     r'C:\Program Files\Derivative\TD\bin\python.exe'],
            listing={self.UV_DIR: ['cpython-3.13.2-windows-x86_64-none']},
            base_prefix=r'C:\Program Files\Derivative\TD\bin')
        self.assertEqual(spec['bases'], [
            r'C:\Program Files\Python311\python.exe',
            r'C:\Users\dev\AppData\Local\Programs\Python\Python312'
            r'\python.exe',
            self.UV_DIR + r'\cpython-3.13.2-windows-x86_64-none\python.exe',
            r'C:\Program Files\Derivative\TD\bin\python.exe',
        ])

    def test_touchdesigner_python_is_the_LAST_base_never_the_first(self):
        """The whole point of the per-user venv is surviving things that
        kill the project venv. A venv built on TouchDesigner's own python
        dies at the next TD upgrade, so it is the floor, not the
        preference -- it still beats a project directory, which dies at
        that PLUS every move, rename and rebuild."""
        spec = self._spec(
            'win32',
            present=[r'C:\Program Files\Python311\python.exe',
                     r'C:\Program Files\Derivative\TD\bin\python.exe'],
            base_prefix=r'C:\Program Files\Derivative\TD\bin')
        self.assertEqual(spec['bases'][-1],
                         r'C:\Program Files\Derivative\TD\bin\python.exe')
        self.assertEqual(spec['bases'][0],
                         r'C:\Program Files\Python311\python.exe')

    def test_the_touchdesigner_base_is_never_listed_twice(self):
        """TD's own python can also be the uv-managed one that built the
        environment; a duplicate would cost a second 15 s probe."""
        uv_python = (self.UV_DIR
                     + r'\cpython-3.11-windows-x86_64-none\python.exe')
        spec = self._spec(
            'win32', present=[uv_python],
            listing={self.UV_DIR: ['cpython-3.11-windows-x86_64-none']},
            base_prefix=self.UV_DIR + r'\cpython-3.11-windows-x86_64-none')
        self.assertEqual(spec['bases'], [uv_python])

    def test_windows_bases_climb_from_the_lowest_supported_minor(self):
        """Deliberately NOT newest-wins. The builder picks ONE base and
        does not retry, and the one thing it must then install is a
        cryptography wheel -- the oldest supported minor is the likeliest
        to have one."""
        spec = self._spec(
            'win32',
            present=[r'C:\Program Files\Python314\python.exe',
                     r'C:\Program Files\Python311\python.exe'])
        self.assertEqual(spec['bases'], [
            r'C:\Program Files\Python311\python.exe',
            r'C:\Program Files\Python314\python.exe',
        ])

    def test_the_minor_order_is_GLOBAL_not_per_install_root(self):
        """The version wins, not the install root. Ranking a whole root
        before the next puts an all-users 3.14 above a per-user 3.11 --
        and since exactly ONE base is built on and never retried, that
        hands the fallback to the minor least likely to have a
        cryptography wheel. The first draft used two ProgramFiles paths
        and so pinned the per-root order without noticing."""
        spec = self._spec(
            'win32',
            present=[r'C:\Program Files\Python314\python.exe',
                     r'C:\Users\dev\AppData\Local\Programs\Python'
                     r'\Python311\python.exe'])
        self.assertEqual(spec['bases'][0],
                         r'C:\Users\dev\AppData\Local\Programs\Python'
                         r'\Python311\python.exe')

    def test_an_interpreter_below_the_floor_is_never_offered(self):
        """3.9 is on this repo's own dev box. Offering it would spend a
        subprocess to be told what the directory name already said."""
        spec = self._spec(
            'win32',
            present=[r'C:\Program Files\Python39\python.exe',
                     self.UV_DIR + r'\cpython-3.9.25-windows-x86_64-none'
                                   r'\python.exe'],
            listing={self.UV_DIR: ['cpython-3.9.25-windows-x86_64-none']})
        self.assertEqual(spec['bases'], [])

    def test_an_unset_APPDATA_scans_nothing_instead_of_the_CWD(self):
        """The uv root used to be joined unguarded, so an empty APPDATA
        produced the RELATIVE 'uv\\python' and _uv_python_bases listdir'd
        whatever that resolves to under the process's current directory
        -- which for a TouchDesigner session is the user's project. The
        TouchDesigner rung two lines below was already guarded this way;
        this one was not.

        The listing is keyed on the relative path on purpose: if the
        guard goes, this is exactly what the scan reaches, and it has to
        be a test failure rather than a silent CWD read.
        """
        env = dict(self.WIN_ENV)
        env['APPDATA'] = ''
        relative = r'uv\python'
        listing = {relative: ['cpython-3.12.13-windows-x86_64-none']}
        present = [relative + r'\cpython-3.12.13-windows-x86_64-none'
                              r'\python.exe']
        spec = self._spec('win32', present=present, listing=listing, env=env)
        self.assertEqual(
            spec['bases'], [],
            'an unset APPDATA scanned a CWD-relative uv root: %r'
            % (spec['bases'],))

    def test_a_MISSING_APPDATA_key_is_the_same_as_an_empty_one(self):
        """`env.get` returns None rather than '' when the variable was
        never set at all -- the commoner case in a service context, and
        the one a truthiness guard has to cover too."""
        env = {key: value for key, value in self.WIN_ENV.items()
               if key != 'APPDATA'}
        relative = r'uv\python'
        spec = self._spec(
            'win32', env=env,
            present=[relative + r'\cpython-3.12.13-windows-x86_64-none'
                                r'\python.exe'],
            listing={relative: ['cpython-3.12.13-windows-x86_64-none']})
        self.assertEqual(spec['bases'], [])

    def test_uv_variants_that_cannot_carry_the_wheel_are_skipped(self):
        """Free-threaded, pypy and graalpy builds are real entries in a
        real uv python dir and the least likely to have a cryptography
        wheel."""
        listing = {self.UV_DIR: [
            'cpython-3.13.14+freethreaded-windows-x86_64-none',
            'pypy-3.11.15-windows-x86_64-none',
            'graalpy-3.12.0-windows-x86_64-none',
            'cpython-3.12.13-windows-x86_64-none',
        ]}
        present = [self.UV_DIR + '\\' + name + r'\python.exe'
                   for name in listing[self.UV_DIR]]
        spec = self._spec('win32', present=present, listing=listing)
        self.assertEqual(
            spec['bases'],
            [self.UV_DIR
             + r'\cpython-3.12.13-windows-x86_64-none\python.exe'])

    def test_uv_bases_climb_from_the_lowest_supported_minor_too(self):
        """The same lowest-first rule the python.org window follows. A
        real uv python dir also carries the minor ALIAS beside the exact
        version (cpython-3.11-... and cpython-3.11.15-...), and both are
        usable bases, so ties must stay stable rather than reorder."""
        listing = {self.UV_DIR: [
            'cpython-3.13.2-windows-x86_64-none',
            'cpython-3.11-windows-x86_64-none',
            'cpython-3.11.15-windows-x86_64-none',
        ]}
        present = [self.UV_DIR + '\\' + name + r'\python.exe'
                   for name in listing[self.UV_DIR]]
        spec = self._spec('win32', present=present, listing=listing)
        self.assertEqual(spec['bases'], [
            self.UV_DIR + r'\cpython-3.11-windows-x86_64-none\python.exe',
            self.UV_DIR + r'\cpython-3.11.15-windows-x86_64-none\python.exe',
            self.UV_DIR + r'\cpython-3.13.2-windows-x86_64-none\python.exe',
        ])

    def test_a_uv_directory_that_is_only_a_name_is_not_a_base(self):
        """The listing is a claim; the file has to be there."""
        spec = self._spec(
            'win32', present=[],
            listing={self.UV_DIR: ['cpython-3.12.13-windows-x86_64-none']})
        self.assertEqual(spec['bases'], [])

    def test_no_base_at_all_is_an_empty_list_not_a_crash(self):
        """A machine with only the Store alias stub. The caller falls
        back to the project venv; nothing here may raise."""
        spec = self._spec('win32')
        self.assertEqual(spec['bases'], [])
        self.assertTrue(spec['dir'])

    # -- macOS, unchanged -----------------------------------------------

    def test_macos_bases_are_homebrew_arm64_then_intel(self):
        spec = self._spec('darwin',
                          present=['/opt/homebrew/bin/python3',
                                   '/usr/local/bin/python3'])
        self.assertEqual(spec['bases'], ['/opt/homebrew/bin/python3',
                                         '/usr/local/bin/python3'])

    def test_macos_offers_apples_python3_only_with_the_tools_installed(self):
        """Spawning the bare /usr/bin/python3 shim WITHOUT the Command
        Line Tools pops Apple's interactive installer -- from a
        background worker, behind the user."""
        without = self._spec('darwin', present=['/usr/bin/python3'])
        self.assertEqual(without['bases'], [])
        with_tools = self._spec(
            'darwin', present=['/usr/bin/python3'],
            dirs=['/Library/Developer/CommandLineTools'])
        self.assertEqual(with_tools['bases'], ['/usr/bin/python3'])

    def test_macos_does_not_gain_windows_bases(self):
        """The darwin ladder is exercised on real Macs and nowhere else,
        so the Windows enumeration must not leak into it."""
        spec = self._spec(
            'darwin',
            present=[r'C:\Program Files\Python311\python.exe'],
            env={'ProgramFiles': r'C:\Program Files',
                 'APPDATA': r'C:\Users\dev\AppData\Roaming'},
            base_prefix='/Applications/TouchDesigner.app/Contents/MacOS')
        self.assertEqual(spec['bases'], [])


# -- 8b. the daemon interpreter must be WINDOWLESS ---------------------
#
# THE FIELD DEFECT, measured: uv 0.11.x and earlier write BYTE-IDENTICAL
# trampolines for a venv's Scripts/python.exe and Scripts/pythonw.exe --
# both PE subsystem CONSOLE, both re-launching the base CONSOLE
# python.exe (astral-sh/uv#19226, fixed in uv 0.12.4). The Scheduled Task
# launches the "windowless" one at logon, so an empty Windows Terminal
# window appears at every single login. A fixed uv repairs NOTHING that
# is already on disk, so the gate and the repair live in our installer.

class TestPESubsystem(EmbodyTestCase):
    """The four bytes that decide whether a window appears at logon.

    A file NAMED pythonw.exe is a claim. The PE subsystem field is the
    fact, and this is the only thing in the codebase that can tell them
    apart -- so it is tested against both PE32 and PE32+ layouts and
    against every malformed shape it must answer None for rather than
    raise, because every caller is on a worker thread.
    """

    def test_it_reads_gui_and_console_out_of_a_pe32_image(self):
        with _TempDir() as root:
            gui = _write(os.path.join(root, 'pythonw.exe'), _gui_pe())
            console = _write(os.path.join(root, 'python.exe'), _console_pe())
            self.assertEqual(install_mod.pe_subsystem(gui),
                             install_mod.PE_SUBSYSTEM_GUI)
            self.assertEqual(install_mod.pe_subsystem(console),
                             install_mod.PE_SUBSYSTEM_CONSOLE)

    def test_it_reads_a_pe32_plus_image_too(self):
        """x64 CPython is PE32+ (magic 0x020b). Subsystem sits at the
        same optional-header offset in both layouts -- everything that
        differs in size comes after it -- and a reader that got that
        wrong would answer 0 for every 64-bit binary on the machine."""
        with _TempDir() as root:
            for name, subsystem in (('w.exe', install_mod.PE_SUBSYSTEM_GUI),
                                    ('c.exe',
                                     install_mod.PE_SUBSYSTEM_CONSOLE)):
                path = _write(os.path.join(root, name),
                              _pe_bytes(subsystem, magic=0x020B))
                self.assertEqual(install_mod.pe_subsystem(path), subsystem)

    def test_a_far_pe_header_is_still_found(self):
        with _TempDir() as root:
            path = _write(os.path.join(root, 'far.exe'),
                          _pe_bytes(install_mod.PE_SUBSYSTEM_GUI,
                                    e_lfanew=0x400))
            self.assertEqual(install_mod.pe_subsystem(path),
                             install_mod.PE_SUBSYSTEM_GUI)

    def test_anything_that_is_not_a_readable_pe_answers_none(self):
        with _TempDir() as root:
            truncated = _pe_bytes(install_mod.PE_SUBSYSTEM_GUI)[:0x80 + 40]
            cases = {
                'text.txt': b'#!/bin/sh\necho hello\n',
                'empty.exe': b'',
                'short.exe': b'MZ',
                'truncated.exe': truncated,
                'past_eof.exe': _pe_bytes(install_mod.PE_SUBSYSTEM_GUI,
                                          e_lfanew=0x9000, size=0x200),
                'bad_signature.exe': _pe_bytes(
                    install_mod.PE_SUBSYSTEM_GUI).replace(b'PE\0\0',
                                                          b'NE\0\0', 1),
                'bad_magic.exe': _pe_bytes(install_mod.PE_SUBSYSTEM_GUI,
                                           magic=0x0107),
            }
            for name, payload in cases.items():
                path = _write(os.path.join(root, name), payload)
                self.assertIsNone(install_mod.pe_subsystem(path),
                                  '%s must read as unknown, not as a PE'
                                  % (name,))
            self.assertIsNone(install_mod.pe_subsystem(
                os.path.join(root, 'not-here.exe')))
            self.assertIsNone(install_mod.pe_subsystem(root),
                              'a directory must not raise IsADirectoryError '
                              'onto a worker thread')
            self.assertIsNone(install_mod.pe_subsystem(None))
            self.assertIsNone(install_mod.pe_subsystem(''))

    def test_a_console_binary_fails_the_windowless_assertion(self):
        """A GUARD ON THE GUARD. Every check added in this change reads
        `pe_subsystem(x) == PE_SUBSYSTEM_GUI`; if the crafted fixtures
        below were somehow all GUI, every one of those tests would pass
        while proving nothing. So: a crafted CONSOLE image must actually
        FAIL that assertion, and the two constants must differ."""
        with _TempDir() as root:
            console = _write(os.path.join(root, 'pythonw.exe'),
                             _console_pe())
            self.assertNotEqual(install_mod.PE_SUBSYSTEM_GUI,
                                install_mod.PE_SUBSYSTEM_CONSOLE)
            self.assertNotEqual(install_mod.pe_subsystem(console),
                                install_mod.PE_SUBSYSTEM_GUI)


class TestEnsureWindowlessDaemonPython(EmbodyTestCase):
    """Repairing a venv whose windowless interpreter is not windowless.

    Real files under a temp dir on every runner: the function reads and
    writes an actual filesystem, and `platform='win32'` is injected, so
    the macOS leg exercises exactly the same decisions on its own disk.
    """

    CONSOLE = install_mod.PE_SUBSYSTEM_CONSOLE
    GUI = install_mod.PE_SUBSYSTEM_GUI

    def _venv(self, root, subsystem=None, home=None, pythonw=True,
              scripts=True):
        venv_dir = os.path.join(root, 'runtime-venv')
        scripts_dir = os.path.join(venv_dir, 'Scripts')
        if scripts:
            os.makedirs(scripts_dir, exist_ok=True)
            _write(os.path.join(scripts_dir, 'python.exe'),
                   _console_pe(b'uv console trampoline'))
            if pythonw:
                _write(os.path.join(scripts_dir, 'pythonw.exe'),
                       _pe_bytes(self.CONSOLE if subsystem is None
                                 else subsystem, tail=b'uv trampoline'))
        else:
            os.makedirs(venv_dir, exist_ok=True)
        if home is not None:
            with open(os.path.join(venv_dir, 'pyvenv.cfg'), 'w',
                      encoding='utf-8') as f:
                f.write('home = %s\nversion = 3.11.15\nuv = 0.11.29\n'
                        % (home,))
        return venv_dir

    # The DLL set a real uv-managed CPython base actually ships on this
    # machine -- including vcruntime140_threads.dll, which an earlier
    # version of the copier neither copied nor mentioned.
    BASE_DLLS = ('python311.dll', 'python3.dll', 'vcruntime140.dll',
                 'vcruntime140_1.dll', 'vcruntime140_threads.dll')

    def _base(self, root, name='base', redirector=True, gui_exe=True,
              dlls=BASE_DLLS):
        base = os.path.join(root, name)
        os.makedirs(base, exist_ok=True)
        _write(os.path.join(base, 'python.exe'), _console_pe(b'base console'))
        if gui_exe:
            _write(os.path.join(base, 'pythonw.exe'), _gui_pe(b'base gui'))
        for dll in dlls:
            _write(os.path.join(base, dll), b'dll ' + dll.encode('ascii'))
        if redirector:
            _write(os.path.join(base, *install_mod.REDIRECTOR_PARTS),
                   _gui_pe(b'cpython redirector'))
        return os.path.join(base, 'python.exe')

    def _fix(self, venv_dir, base_python=None, **kw):
        kw.setdefault('platform', 'win32')
        return install_mod.ensure_windowless_daemon_python(
            venv_dir, base_python, **kw)

    def _scripts(self, venv_dir):
        return os.path.join(venv_dir, 'Scripts')

    def _names(self, venv_dir):
        return sorted(os.listdir(self._scripts(venv_dir)))

    def _daemon(self, venv_dir):
        return os.path.join(self._scripts(venv_dir), 'pythonw.exe')

    def _interpreter_files(self, venv_dir):
        """{name: bytes} for everything EXCEPT our own sweepable leftovers.

        WHAT A REFUSAL PROMISES, precisely. It is not a total no-op: the
        sweep of this module's own *.old-/*.tmp- files runs first, on
        every call, and has to (a leftover is only ever produced by a
        repair that ENDS with the venv windowless, so a sweep that ran
        later than the fast path would never run at all). What a refusal
        does promise is that the interpreter file set is untouched -- no
        new exe, no new DLL, no altered pythonw.exe.
        """
        found = {}
        for name in sorted(_listdir(self._scripts(venv_dir))):
            if install_mod._STALE_REPAIR_NAME.match(name):
                continue
            found[name] = _read(os.path.join(self._scripts(venv_dir), name))
        return found

    # -- plan A: CPython's own redirector -------------------------------

    def test_the_redirector_is_preferred_and_copies_ONE_file(self):
        """It needs zero sibling DLLs (it resolves the base through the
        venv's pyvenv.cfg) and reports the VENV path as sys.executable --
        which the generated launcher requires, or it refuses to start."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            before = self._names(venv_dir)
            got = self._fix(venv_dir, base)
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['repaired'])
            self.assertEqual(got['plan'], 'redirector')
            self.assertEqual([os.path.basename(p) for p in got['copied']],
                             ['pythonw.exe'])
            self.assertEqual(self._names(venv_dir), before,
                             'plan A adds no DLLs beside the interpreter')
            self.assertEqual(install_mod.pe_subsystem(self._daemon(venv_dir)),
                             self.GUI)
            self.assertEqual(
                _read(self._daemon(venv_dir)),
                _read(os.path.join(os.path.dirname(base),
                                   *install_mod.REDIRECTOR_PARTS)),
                'the redirector itself must be what landed')

    def test_pyvenv_cfg_names_the_base_when_the_caller_cannot(self):
        """The reuse path repairs a venv it did not build, so it has no
        base to pass. pyvenv.cfg's `home` is the same answer the repaired
        interpreter uses at run time -- not a guess."""
        with _TempDir() as root:
            base = self._base(root)
            venv_dir = self._venv(root, home=os.path.dirname(base))
            got = self._fix(venv_dir)
            self.assertTrue(got['ok'], got)
            self.assertEqual(got['plan'], 'redirector')
            self.assertEqual(install_mod.pe_subsystem(self._daemon(venv_dir)),
                             self.GUI)

    def test_a_venv_with_no_pyvenv_cfg_refuses_without_touching_it(self):
        with _TempDir() as root:
            self._base(root)
            venv_dir = self._venv(root)
            before = self._interpreter_files(venv_dir)
            got = self._fix(venv_dir)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'],
                             'daemon_venv_repair_source_missing')
            self.assertEqual(self._interpreter_files(venv_dir), before)

    def test_a_bom_in_pyvenv_cfg_still_names_the_base(self):
        """Some editors and installers write one. Read as plain utf-8 the
        BOM stays glued to the first key, `home` never matches, and every
        reuse repair on that machine becomes a false 'cannot locate the
        base Python' refusal -- reported as a console window nobody can
        get rid of."""
        with _TempDir() as root:
            base = self._base(root)
            venv_dir = self._venv(root)
            with open(os.path.join(venv_dir, 'pyvenv.cfg'), 'wb') as f:
                f.write(b'\xef\xbb\xbfhome = %s\nversion = 3.11.15\n'
                        % (os.path.dirname(base).encode('utf-8'),))
            got = self._fix(venv_dir)
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['repaired'])
            self.assertEqual(install_mod.pe_subsystem(self._daemon(venv_dir)),
                             self.GUI)

    def test_a_venv_dir_that_is_not_a_string_refuses_instead_of_raising(self):
        """NEVER RAISES is the contract every caller leans on -- this runs
        on a worker thread, and a foreign spec carrying a Path or a None
        must come back as a refusal, not a TypeError out of os.path.join."""
        for bad in (None, 0, b'bytes', ['list'], object()):
            got = self._fix(bad)
            self.assertFalse(got['ok'], bad)
            self.assertEqual(got['reason'],
                             'daemon_venv_repair_source_missing')

    # -- plan B: the base's own GUI exe plus its DLLs --------------------

    def test_without_a_redirector_the_gui_exe_and_its_dlls_are_copied(self):
        """A lone pythonw.exe exits 0xC0000135 (STATUS_DLL_NOT_FOUND)
        SILENTLY -- which looks exactly like a healthy daemon."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root, redirector=False)
            got = self._fix(venv_dir, base)
            self.assertTrue(got['ok'], got)
            self.assertEqual(got['plan'], 'dll_copy')
            self.assertEqual(got['note'], '')
            self.assertEqual(
                sorted(os.path.basename(p) for p in got['copied']),
                ['python3.dll', 'python311.dll', 'pythonw.exe',
                 'vcruntime140.dll', 'vcruntime140_1.dll',
                 'vcruntime140_threads.dll'])
            self.assertEqual(install_mod.pe_subsystem(self._daemon(venv_dir)),
                             self.GUI)
            self.assertEqual(_read(os.path.join(self._scripts(venv_dir),
                                                'python311.dll')),
                             b'dll python311.dll')

    def test_the_versioned_dll_is_globbed_never_named(self):
        """python311.dll on one machine, python313.dll on the next. A
        hardcoded name would silently copy an interpreter that cannot
        start."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root, redirector=False,
                              dlls=('python313.dll', 'python3.dll'))
            got = self._fix(venv_dir, base)
            self.assertTrue(got['ok'], got)
            self.assertIn('python313.dll',
                          [os.path.basename(p) for p in got['copied']])
            self.assertIn('python313.dll', self._names(venv_dir))

    def test_a_missing_vcruntime_is_tolerated_but_said_out_loud(self):
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root, redirector=False,
                              dlls=('python311.dll', 'python3.dll'))
            got = self._fix(venv_dir, base)
            self.assertTrue(got['ok'], got)
            self.assertIn('vcruntime140.dll', got['note'])
            self.assertEqual(install_mod.pe_subsystem(self._daemon(venv_dir)),
                             self.GUI)

    def test_a_console_redirector_falls_through_to_the_dll_copy(self):
        """The redirector is preferred because of what it IS, not where
        it sits: a console one is no better than the trampoline."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            _write(os.path.join(os.path.dirname(base),
                                *install_mod.REDIRECTOR_PARTS),
                   _console_pe(b'wrong subsystem'))
            got = self._fix(venv_dir, base)
            self.assertTrue(got['ok'], got)
            self.assertEqual(got['plan'], 'dll_copy')

    # -- refusals leave the venv exactly as it was ----------------------

    def test_no_versioned_dll_refuses_and_writes_nothing(self):
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root, redirector=False, dlls=('python3.dll',))
            before = self._interpreter_files(venv_dir)
            got = self._fix(venv_dir, base)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'],
                             'daemon_venv_repair_source_missing')
            self.assertIn('python3XX.dll', got['detail'])
            self.assertEqual(self._interpreter_files(venv_dir), before,
                             'a refusal must not half-repair the venv: no '
                             'new DLL, no new exe, no altered interpreter')

    def test_a_base_with_no_windowless_python_at_all_refuses(self):
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root, redirector=False, gui_exe=False)
            before = self._names(venv_dir)
            got = self._fix(venv_dir, base)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'],
                             'daemon_venv_repair_source_missing')
            self.assertEqual(self._names(venv_dir), before)

    def test_a_console_pythonw_in_the_base_is_not_a_source(self):
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root, redirector=False)
            _write(os.path.join(os.path.dirname(base), 'pythonw.exe'),
                   _console_pe(b'console base gui slot'))
            got = self._fix(venv_dir, base)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'],
                             'daemon_venv_repair_source_missing')

    def test_a_venv_with_no_scripts_directory_refuses(self):
        with _TempDir() as root:
            self._base(root)
            venv_dir = self._venv(root, scripts=False)
            got = self._fix(venv_dir)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'],
                             'daemon_venv_repair_source_missing')
            self.assertFalse(os.path.isdir(self._scripts(venv_dir)),
                             'a refusal must not create a Scripts dir')

    def test_a_destination_outside_the_venv_is_refused(self):
        """An interrupted repair or a hand-made junction must not turn
        this into a writer of arbitrary paths."""
        with _TempDir() as root:
            base = self._base(root)
            venv_dir = os.path.join(root, 'runtime-venv')
            outside = os.path.join(root, 'elsewhere')
            os.makedirs(venv_dir)
            os.makedirs(outside)
            _write(os.path.join(outside, 'pythonw.exe'),
                   _console_pe(b'redirected'))
            try:
                os.symlink(outside, os.path.join(venv_dir, 'Scripts'),
                           target_is_directory=True)
            except (OSError, NotImplementedError, AttributeError):
                self.skipTest('this runner cannot create directory symlinks')
            got = self._fix(venv_dir, base)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'daemon_venv_repair_unsafe_path')
            self.assertEqual(install_mod.pe_subsystem(
                os.path.join(outside, 'pythonw.exe')), self.CONSOLE,
                'the redirected file must not have been written')

    # -- the fast path, and the file that must never be touched ---------

    def test_an_already_windowless_interpreter_is_left_alone(self):
        with _TempDir() as root:
            venv_dir = self._venv(root, subsystem=self.GUI)
            self._base(root)
            before = _read(self._daemon(venv_dir))
            got = self._fix(venv_dir, os.path.join(root, 'base',
                                                   'python.exe'))
            self.assertTrue(got['ok'], got)
            self.assertFalse(got['repaired'])
            self.assertEqual(got['plan'], '')
            self.assertEqual(got['copied'], [])
            self.assertEqual(_read(self._daemon(venv_dir)), before)

    def test_the_console_python_exe_is_never_touched(self):
        """uv pip is driven through Scripts/python.exe over pipes; it is
        SUPPOSED to be a console binary, and replacing it would break the
        next dependency install."""
        with _TempDir() as root:
            for redirector in (True, False):
                venv_dir = self._venv(root)
                base = self._base(root, name='base-%s' % (redirector,),
                                  redirector=redirector)
                console = os.path.join(self._scripts(venv_dir), 'python.exe')
                before = _read(console)
                got = self._fix(venv_dir, base)
                self.assertTrue(got['ok'], got)
                self.assertEqual(_read(console), before)
                self.assertNotIn('python.exe',
                                 [os.path.basename(p) for p in got['copied']])
                shutil.rmtree(venv_dir)

    def test_nothing_temporary_survives_a_successful_repair(self):
        with _TempDir() as root:
            venv_dir = self._venv(root)
            got = self._fix(venv_dir, self._base(root, redirector=False))
            self.assertTrue(got['ok'], got)
            self.assertEqual([n for n in self._names(venv_dir)
                              if '.tmp-' in n], [])

    def test_non_win32_is_a_no_op(self):
        """posix has no windowless twin to get wrong -- and this must not
        start rewriting a macOS venv's bin/python3."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            before = _read(self._daemon(venv_dir))
            got = self._fix(venv_dir, self._base(root), platform='darwin')
            self.assertTrue(got['ok'], got)
            self.assertFalse(got['applicable'])
            self.assertFalse(got['repaired'])
            self.assertEqual(_read(self._daemon(venv_dir)), before)

    # -- the daemon is RUNNING while this happens -----------------------

    def test_a_locked_interpreter_is_renamed_aside_not_overwritten(self):
        """A running exe cannot be replaced or deleted on Windows, but it
        CAN be renamed -- and the daemon is normally running during an
        install. The seams are injected rather than patched onto os:
        globally patching os in this suite has burned us before, and the
        real denial only happens against a live process."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)

            def replace(source, destination):
                if os.path.basename(destination) == 'pythonw.exe':
                    raise PermissionError(13, 'in use by another process')
                return os.replace(source, destination)

            def unlink(path):
                if install_mod.STALE_DAEMON_PREFIX in os.path.basename(path):
                    raise PermissionError(13, 'in use by another process')
                return os.unlink(path)

            got = self._fix(venv_dir, base, replace=replace, unlink=unlink)
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['repaired'])
            self.assertEqual(install_mod.pe_subsystem(target), self.GUI)
            leftovers = [n for n in self._names(venv_dir)
                         if n.startswith(install_mod.STALE_DAEMON_PREFIX)]
            self.assertEqual(len(leftovers), 1, self._names(venv_dir))
            self.assertEqual([os.path.basename(p) for p in got['kept']],
                             leftovers,
                             'the leftover must be REPORTED, not hidden')
            self.assertEqual([n for n in self._names(venv_dir)
                              if '.tmp-' in n], [])

    def test_the_leftover_of_a_locked_repair_is_swept_by_the_NEXT_call(self):
        """THE REAL SEQUENCE, end to end -- and the one an earlier version
        of this test faked.

        A leftover is produced by exactly one thing: a repair over a LIVE
        daemon, which renames the running image aside and CANNOT delete
        it. That sequence ends with the venv already windowless, so a
        sweep that ran after the GUI fast path would never run again on
        that machine -- the leftover would be permanent and the note
        promising 'removed on the next repair' false forever. Seeding a
        console venv hid exactly that, because it is a state a successful
        repair never leaves behind."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)

            def replace(source, destination):
                raise PermissionError(13, 'in use by another process')

            def unlink(path):
                if install_mod._STALE_REPAIR_NAME.match(
                        os.path.basename(path)):
                    raise PermissionError(13, 'in use by another process')
                return os.unlink(path)

            first = self._fix(venv_dir, base, replace=replace, unlink=unlink)
            self.assertTrue(first['ok'], first)
            self.assertTrue(first['repaired'])
            leftover = first['kept'][-1]
            self.assertTrue(os.path.isfile(leftover))
            self.assertEqual(install_mod.pe_subsystem(self._daemon(venv_dir)),
                             self.GUI, 'the repair left the venv WINDOWLESS '
                                       '-- which is why the sweep may not '
                                       'live behind the GUI fast path')

            # The next install: nothing to repair, everything to tidy.
            second = self._fix(venv_dir, base)
            self.assertTrue(second['ok'], second)
            self.assertFalse(second['repaired'],
                             'the fast path still refuses to touch a '
                             'windowless interpreter')
            self.assertFalse(os.path.exists(leftover),
                             'the promise made in the first result must '
                             'come true on the next call')
            self.assertEqual(second['kept'], [])

    def test_the_sweep_takes_our_leftovers_and_leaves_human_files(self):
        """Both shapes this module can orphan -- the renamed-aside image
        and a staging copy a kill left behind (multi-MB, and nothing else
        would ever remove it) -- in either case.

        And ONLY those. The tail must look like time.time_ns(), which has
        been 19 digits since 2001, precisely so a person's date-stamped
        backup (`pythonw.exe.old-20260817-143000`) cannot match: an
        earlier \\d+-\\d+ tail swept exactly that."""
        with _TempDir() as root:
            venv_dir = self._venv(root, subsystem=self.GUI)
            scripts = self._scripts(venv_dir)
            swept = [
                _write(os.path.join(
                    scripts,
                    install_mod.STALE_DAEMON_PREFIX + '4242-1787005143889438000'),
                    _console_pe(b'yesterday')),
                _write(os.path.join(
                    scripts, 'python311.dll.old-4242-1787005143889438001'),
                    b'yesterday dll'),
                _write(os.path.join(
                    scripts, 'pythonw.exe.tmp-7-1787005143889438002'),
                    _gui_pe(b'staged then killed')),
                _write(os.path.join(
                    scripts, 'PYTHONW.EXE.OLD-8-1787005143889438003'),
                    b'shouting'),
            ]
            keepers = [
                _write(os.path.join(scripts, 'notes.old-backup.txt'),
                       b'a person put this here'),
                _write(os.path.join(scripts, 'pythonw.exe.old'),
                       b'a person renamed this by hand'),
                _write(os.path.join(scripts, 'pythonw.exe.old-mine'),
                       b'no pid-ns tail: not ours'),
                _write(os.path.join(scripts,
                                    'pythonw.exe.old-20260817-143000'),
                       b'a person date-stamped this'),
                _write(os.path.join(scripts, 'python311.dll.old-1-2'),
                       b'too short to be a nanosecond clock'),
            ]
            got = self._fix(venv_dir, self._base(root))
            self.assertTrue(got['ok'], got)
            for path in swept:
                self.assertFalse(os.path.exists(path), path)
            for path in keepers:
                self.assertTrue(os.path.exists(path), path)
            self.assertEqual(got['kept'], [])

    def test_the_sweep_never_reaches_outside_a_junctioned_scripts(self):
        """It runs BEFORE the per-destination escape guard, so it carries
        its own: a Scripts that resolves outside the venv gets no unlinks
        at all, however well-named the files there are."""
        with _TempDir() as root:
            base = self._base(root)
            venv_dir = os.path.join(root, 'runtime-venv')
            outside = os.path.join(root, 'elsewhere')
            os.makedirs(venv_dir)
            os.makedirs(outside)
            _write(os.path.join(outside, 'pythonw.exe'),
                   _console_pe(b'redirected'))
            bait = _write(
                os.path.join(outside,
                             'pythonw.exe.old-4242-1787005143889438000'),
                b'sweep-shaped, and still not ours to delete')
            try:
                os.symlink(outside, os.path.join(venv_dir, 'Scripts'),
                           target_is_directory=True)
            except (OSError, NotImplementedError, AttributeError):
                self.skipTest('this runner cannot create directory symlinks')
            got = self._fix(venv_dir, base)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'daemon_venv_repair_unsafe_path')
            self.assertTrue(os.path.exists(bait),
                            'a junctioned Scripts must not aim the sweep at '
                            'files outside the venv')

    def _denied(self, *matchers):
        """A rename/replace seam that denies the named moves, counts calls.

        Sleep is injected alongside every use of this, so the ten-attempt
        retry loop is exercised at full length and costs nothing -- a real
        backoff here would be a wall-clock assertion, which this repo has
        been burned by on shared CI runners.
        """
        calls = []

        def rename(source, destination):
            calls.append((os.path.basename(source),
                          os.path.basename(destination)))
            for match_source, match_destination in matchers:
                if (match_source in os.path.basename(source)
                        and match_destination in os.path.basename(
                            destination)):
                    raise PermissionError(13, 'in use by another process')
            return os.rename(source, destination)

        return rename, calls

    def test_a_denied_forward_move_is_rolled_back_with_the_same_budget(self):
        """(i) The replacement cannot take the freed name, but the
        ORIGINAL can go back. A rollback with a smaller retry budget than
        the move it undoes is how one transient denial turns into a venv
        with no interpreter at all."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)
            before = _read(target)
            slept = []
            rename, calls = self._denied(('.tmp-', 'pythonw.exe'))

            def replace(source, destination):
                raise PermissionError(13, 'in use by another process')

            got = self._fix(venv_dir, base, replace=replace, rename=rename,
                            sleep=slept.append)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'daemon_venv_repair_locked')
            self.assertIn('restored', got['detail'])
            self.assertEqual(_read(target), before,
                             'the original interpreter must be back at the '
                             'canonical name, byte for byte')
            self.assertEqual([n for n in self._names(venv_dir)
                              if '.tmp-' in n], [],
                             'the staged copy must not outlive the refusal')
            self.assertEqual(len(slept), 9,
                             'the forward move gets ten attempts (nine '
                             'backoffs) -- previously untested')

    def test_when_rollback_fails_too_the_repair_takes_the_free_name(self):
        """(ii) ANY interpreter at the canonical name beats none. The path
        in the Scheduled Task's <Command> and in installed.json is that
        exact string; leaving it empty is the one outcome worse than a
        console window."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)
            attempts = {'forward': 0}

            def replace(source, destination):
                raise PermissionError(13, 'in use by another process')

            def rename(source, destination):
                name = os.path.basename(source)
                if '.old-' in name:
                    raise PermissionError(13, 'rollback denied')
                if '.tmp-' in name:
                    attempts['forward'] += 1
                    # Denied for the whole first budget, allowed on the
                    # last-ditch attempt after the rollback also failed.
                    if attempts['forward'] <= 10:
                        raise PermissionError(13, 'forward denied')
                return os.rename(source, destination)

            got = self._fix(venv_dir, base, replace=replace, rename=rename,
                            sleep=lambda seconds: None)
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['repaired'])
            self.assertTrue(os.path.isfile(target))
            self.assertEqual(install_mod.pe_subsystem(target), self.GUI)
            self.assertEqual([os.path.basename(p) for p in got['kept']],
                             [n for n in self._names(venv_dir)
                              if n.startswith(
                                  install_mod.STALE_DAEMON_PREFIX)])

    def test_an_interpreter_lost_to_a_double_denial_is_named_as_lost(self):
        """(iii) Nothing could take the canonical name. The refusal must
        say THAT -- 'still a console interpreter' would send the next
        diagnosis hunting a window that cannot appear -- and it must name
        where the old image sits, or a rebuild is the only way out."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)

            def replace(source, destination):
                raise PermissionError(13, 'in use by another process')

            def rename(source, destination):
                if os.path.basename(destination) == 'pythonw.exe':
                    raise PermissionError(13, 'denied both ways')
                return os.rename(source, destination)

            got = self._fix(venv_dir, base, replace=replace, rename=rename,
                            sleep=lambda seconds: None)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'daemon_venv_repair_locked')
            self.assertTrue(got['interpreter_missing'])
            self.assertFalse(os.path.exists(target))
            self.assertIn('no longer exists', got['detail'])
            aside = [n for n in self._names(venv_dir)
                     if n.startswith(install_mod.STALE_DAEMON_PREFIX)]
            self.assertEqual(len(aside), 1)
            self.assertIn(aside[0], got['detail'],
                          'the refusal must name where the old image went')
            self.assertIn(os.path.join(self._scripts(venv_dir), aside[0]),
                          got['kept'])
            self.assertEqual([n for n in self._names(venv_dir)
                              if '.tmp-' in n], [])

    def test_a_concurrent_repair_that_wins_is_reported_as_success(self):
        """Two Embodys, one machine, one per-user venv. If our write is
        denied while ANOTHER process makes the interpreter windowless,
        the honest answer is success -- a refusal would describe a state
        that no longer exists and put a console-window warning in front of
        a user who does not have one."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)

            def replace(source, destination):
                # The other process lands its repair in the same instant.
                _write(target, _gui_pe(b'the other install won'))
                raise PermissionError(13, 'in use by another process')

            def rename(source, destination):
                raise PermissionError(13, 'in use by another process')

            got = self._fix(venv_dir, base, replace=replace, rename=rename,
                            sleep=lambda seconds: None)
            self.assertTrue(got['ok'], got)
            self.assertFalse(got['repaired'],
                             'we did not repair it -- someone else did')
            self.assertTrue(got['concurrent'])
            self.assertEqual(install_mod.pe_subsystem(target), self.GUI)

    # -- unreadable is not console -------------------------------------

    def _unreadable(self, path, misses=99):
        """A pe_subsystem seam that cannot read `path` for `misses` calls.

        Models a file that EXISTS and cannot be opened right now -- a peer
        repair holding it for the instant it is renamed aside, a scanner
        or indexer on a freshly written exe. Injected rather than locked
        for real, so the same branches run on every runner; a real
        share-mode-0 lock proves the model separately below.
        """
        state = {'left': misses}

        def read(candidate):
            if (os.path.normcase(str(candidate))
                    == os.path.normcase(path) and state['left'] > 0):
                state['left'] -= 1
                return None
            return install_mod.pe_subsystem(candidate)

        return read, state

    def test_an_unreadable_interpreter_is_never_treated_as_console(self):
        """THE RACE, reproduced 4/10 rounds with four real processes on
        one venv. pe_subsystem answers None for a file it cannot open,
        and None is not 'console' -- reading it as one starts a repair
        nobody needed, of a file somebody else is holding."""
        with _TempDir() as root:
            venv_dir = self._venv(root, subsystem=self.GUI)
            base = self._base(root)
            target = self._daemon(venv_dir)
            before = _read(target)
            read, _state = self._unreadable(target)
            got = self._fix(venv_dir, base, read_subsystem=read,
                            sleep=lambda seconds: None)
            self.assertTrue(got['ok'], got)
            self.assertFalse(got['repaired'])
            self.assertTrue(got['unverified'])
            self.assertIsNone(got['subsystem'])
            self.assertIn('could not be read', got['note'])
            self.assertEqual(_read(target), before,
                             'a file we cannot even read must not be '
                             'rewritten on the assumption it is wrong')

    def test_a_read_that_settles_within_the_retries_is_believed(self):
        """The lock is usually gone in milliseconds, which is the whole
        reason for retrying rather than refusing on the first None."""
        with _TempDir() as root:
            venv_dir = self._venv(root, subsystem=self.GUI)
            base = self._base(root)
            slept = []
            read, state = self._unreadable(self._daemon(venv_dir), misses=3)
            got = self._fix(venv_dir, base, read_subsystem=read,
                            sleep=slept.append)
            self.assertTrue(got['ok'], got)
            self.assertFalse(got['repaired'])
            self.assertEqual(got['subsystem'], self.GUI,
                             'once it settles, the answer is the fact -- '
                             'not the transient None')
            self.assertEqual(state['left'], 0)
            self.assertEqual(len(slept), 3)

    def test_a_repair_that_cannot_be_read_back_is_not_called_a_failure(self):
        """Every write reported success and the file is there; only the
        read-back is owed. Reporting 'still not a windowless interpreter'
        on the strength of a scanner's lock invents a defect."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)
            calls = {'n': 0}

            def read(candidate):
                # Readable while we decide (console, so repair), blind
                # for the post-repair confirmation.
                if os.path.normcase(str(candidate)) != os.path.normcase(
                        target):
                    return install_mod.pe_subsystem(candidate)
                calls['n'] += 1
                return None if calls['n'] > 1 else self.CONSOLE

            got = self._fix(venv_dir, base, read_subsystem=read,
                            sleep=lambda seconds: None)
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['repaired'])
            self.assertTrue(got['unverified'])
            self.assertIsNone(got['subsystem'])
            self.assertIn('read back', got['note'])
            self.assertEqual(install_mod.pe_subsystem(target), self.GUI,
                             'and the repair really did land')

    def test_a_denied_repair_on_an_unreadable_file_says_unverified(self):
        """The classifier's third case. Whoever denied the write is often
        the same process holding the file, so 'still a console daemon
        interpreter' is a claim with no evidence behind it."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)
            calls = {'n': 0}

            def read(candidate):
                if os.path.normcase(str(candidate)) != os.path.normcase(
                        target):
                    return install_mod.pe_subsystem(candidate)
                calls['n'] += 1
                return None if calls['n'] > 1 else self.CONSOLE

            def replace(source, destination):
                raise PermissionError(13, 'in use by another process')

            def rename(source, destination):
                raise PermissionError(13, 'in use by another process')

            got = self._fix(venv_dir, base, replace=replace, rename=rename,
                            read_subsystem=read,
                            sleep=lambda seconds: None)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'daemon_venv_repair_locked')
            self.assertTrue(got['interpreter_unreadable'])
            self.assertFalse(got['interpreter_missing'])
            self.assertIn('unverified', got['detail'])

    def test_a_real_exclusive_lock_reads_as_unreadable_not_console(self):
        """A GUARD ON THE MODEL. The seam above only proves the branches
        if a genuinely unopenable file really does answer None with
        os.path.isfile still True -- which is what CreateFileW with
        dwShareMode=0 produces, and what the four-process race hit."""
        if sys.platform != 'win32':
            self.skipTest('share-mode locking is a Windows behaviour')
        import ctypes
        from ctypes import wintypes
        with _TempDir() as root:
            path = _write(os.path.join(root, 'pythonw.exe'), _gui_pe())
            self.assertEqual(install_mod.pe_subsystem(path), self.GUI)
            create = ctypes.windll.kernel32.CreateFileW
            create.restype = wintypes.HANDLE
            handle = create(path, 0x80000000, 0, None, 3, 0x80, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value,
                                'could not take the exclusive handle')
            try:
                self.assertIsNone(install_mod.pe_subsystem(path))
                self.assertTrue(os.path.isfile(path),
                                'an exclusively held file is still a file '
                                '-- which is the distinction the settled '
                                'read is built on')
                subsystem, present = install_mod._pe_subsystem_settled(
                    path, sleep=lambda seconds: None)
                self.assertIsNone(subsystem)
                self.assertTrue(present)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            self.assertEqual(install_mod.pe_subsystem(path), self.GUI)

    # -- partial writes, and what a refusal really promises -------------

    def test_a_refusal_part_way_through_a_dll_copy_says_so_honestly(self):
        """A dll_copy denied on its third member has already written the
        first two. The guarantee is about ONE file -- the interpreter the
        Scheduled Task launches -- and the wording must not read as a
        promise about the whole venv. The staged DLLs are inert: nothing
        loads them until a windowless exe joins them."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root, redirector=False)
            target = self._daemon(venv_dir)
            before = _read(target)

            def replace(source, destination):
                if os.path.basename(destination) == 'python3.dll':
                    raise PermissionError(13, 'denied')
                return os.replace(source, destination)

            got = self._fix(venv_dir, base, replace=replace,
                            sleep=lambda seconds: None)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'daemon_venv_repair_locked')
            self.assertEqual([os.path.basename(p) for p in got['copied']],
                             ['python311.dll'])
            self.assertIn('python311.dll', self._names(venv_dir),
                          'the earlier member really did land')
            self.assertEqual(_read(target), before,
                             'THE guarantee: the interpreter the task '
                             'launches is untouched')
            self.assertIn('that file was not changed', got['detail'])
            self.assertNotIn('nothing was changed', got['detail'],
                             'a sentence about one file must not read as '
                             'a promise about the venv')
            self.assertEqual([n for n in self._names(venv_dir)
                              if '.tmp-' in n], [])

    def test_the_sweep_never_takes_the_last_surviving_interpreter(self):
        """The hazard the top-of-function sweep created: install #1 loses
        pythonw.exe to a double denial and leaves the previous image as a
        .old-; install #2 must not tidy that away before discovering it
        cannot repair anything. Sweeping there turns a recoverable state
        into rebuild-or-nothing."""
        with _TempDir() as root:
            venv_dir = self._venv(root, pythonw=False)
            scripts = self._scripts(venv_dir)
            survivor = _write(
                os.path.join(scripts, install_mod.STALE_DAEMON_PREFIX
                             + '4242-1787005143889438000'),
                _console_pe(b'the only interpreter left'))
            got = self._fix(venv_dir, os.path.join(root, 'gone',
                                                   'python.exe'))
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'],
                             'daemon_venv_repair_source_missing')
            self.assertTrue(os.path.isfile(survivor),
                            'tidying may never delete the last copy of the '
                            'interpreter')

    def test_a_locked_file_that_cannot_even_be_renamed_changes_nothing(self):
        """A windowed daemon beats a dead daemon: if the old image cannot
        be moved out of the way, the repair refuses and leaves the venv
        exactly as it found it."""
        with _TempDir() as root:
            venv_dir = self._venv(root)
            base = self._base(root)
            target = self._daemon(venv_dir)
            before = _read(target)

            def replace(source, destination):
                raise PermissionError(13, 'in use by another process')

            def rename(source, destination):
                raise PermissionError(13, 'in use by another process')

            got = self._fix(venv_dir, base, replace=replace, rename=rename)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'daemon_venv_repair_locked')
            self.assertEqual(_read(target), before)
            self.assertEqual([n for n in self._names(venv_dir)
                              if '.tmp-' in n], [])


class TestConvoyManagedRuntime(EmbodyTestCase):

    PLATFORM = 'win32'
    ARCH = 'x86_64'
    RUNTIME_ID = 'cpython-3.11.15-crypto-test-win64'
    PYTHON_REL = 'python/pythonw.exe'
    PROBE_PYTHON_REL = 'python/python.exe'
    CRYPTO_REL = 'Lib/site-packages/cryptography/__init__.py'

    def _payloads(self, daemon_subsystem=None):
        # REAL PE HEADERS for the two exes. The daemon interpreter of a
        # win32 bundle is now gated on its subsystem -- a console one
        # would put an empty terminal on the desktop at every logon -- so
        # a fixture of plain bytes would describe a bundle this installer
        # is right to refuse. `daemon_subsystem` exists for the test that
        # proves it DOES refuse.
        daemon = _pe_bytes(daemon_subsystem or install_mod.PE_SUBSYSTEM_GUI,
                           tail=b'fake self-contained python')
        return {
            self.PYTHON_REL: daemon,
            self.PROBE_PYTHON_REL: _console_pe(
                b'fake self-contained probe python'),
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

    def test_a_console_daemon_interpreter_is_refused_at_provision(self):
        """A bundle whose pythonw.exe is really a console build would put
        an empty terminal on the desktop at every logon, and no amount of
        live cryptography makes that the runtime we install."""
        with _TempDir() as root:
            payloads = self._payloads(
                daemon_subsystem=install_mod.PE_SUBSYSTEM_CONSOLE)
            bundle, digest = self._bundle(root, payloads=payloads)
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'],
                             'runtime_interpreter_not_windowless')
            self.assertIsNone(
                install_mod.read_runtime_receipt(root, self.RUNTIME_ID,
                                                 self.PLATFORM),
                'a refused bundle must never be activated')

    def test_a_console_daemon_interpreter_is_refused_at_verification(self):
        """REFUSED, never repaired. A managed runtime is a hash-pinned
        release artifact: rewriting one of its files would break the
        inventory just verified above it, and the defect belongs to the
        release build. (The daemon venv is the opposite case -- Embody
        builds that one, so Embody repairs it.)"""
        with _TempDir() as root:
            bundle, digest = self._bundle(root)
            got = install_mod.provision_runtime_bundle(
                root, bundle, digest, self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertTrue(got['ok'], got)
            # Swap in a console build AND make the receipt agree, so the
            # integrity hash above passes and this gate is what answers.
            payload = _console_pe(b'console daemon')
            with open(got['interpreter'], 'wb') as f:
                f.write(payload)
            # HOST-filesystem path, not runtime_complete_path: that helper
            # joins with the TARGET platform's separators (ntpath for
            # win32), which are literal filename characters on a POSIX
            # host -- the macOS CI leg cannot open them. Mirror the
            # module's own I/O helper instead.
            receipt_path = os.path.join(
                install_mod._runtime_fs_dir(root, self.RUNTIME_ID),
                install_mod.COMPLETE_FILE)
            with open(receipt_path, encoding='utf-8') as f:
                receipt = json.load(f)
            for row in receipt['files']:
                if row['path'] == self.PYTHON_REL:
                    row['size'] = len(payload)
                    row['sha256'] = hashlib.sha256(payload).hexdigest()
            with open(receipt_path, 'w', encoding='utf-8') as f:
                json.dump(receipt, f)
            verified = install_mod.verify_managed_runtime(
                root, got['interpreter'], self.PLATFORM, self.ARCH,
                runner=self._probe_runner())
            self.assertFalse(verified['ok'])
            self.assertEqual(verified['reason'],
                             'runtime_interpreter_not_windowless')

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
                # A REAL GUI PE header on win32: the installer refuses a
                # bundle whose daemon interpreter is a console binary, so
                # a packager fixture of plain bytes would be describing a
                # release nobody should ship.
                python_rel: (_gui_pe(b'daemon-python')
                             if platform_name == 'win32'
                             else b'daemon-python'),
                # ...and the probe half must be the CONSOLE binary: the
                # packager now checks that both names really are what
                # they claim, because the split is unrepairable once the
                # hash-pinned bundle has shipped.
                probe_rel: (_console_pe(b'probe-python')
                            if platform_name == 'win32'
                            else b'probe-python'),
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

    def test_a_dead_interpreter_authorises_a_runtime_repair(self):
        """host_state says 'Install re-resolves it' the moment the
        recorded Python is gone. Before this branch existed, Install
        answered refuse_downgrade instead -- so the status named a
        button that refused, and pressing it REPLACED the warning with
        'installed by a newer Embody'."""
        got = install_mod.plan_install(self._record('6.0.230'), '6.0.223',
                                       interpreter_exists=False)
        self.assertEqual(got['action'], 'repair_runtime')
        self.assertIn('6.0.230', got['detail'])

    def test_a_LIVE_interpreter_still_refuses_the_downgrade(self):
        """The A-36 guard, and it sits here beside its exception on
        purpose: nothing about a healthy install authorises a
        downgrade."""
        got = install_mod.plan_install(self._record('6.0.230'), '6.0.223',
                                       interpreter_exists=True)
        self.assertEqual(got['action'], 'refuse_downgrade')

    def test_an_UNKNOWN_interpreter_still_refuses_the_downgrade(self):
        """None is not evidence. A caller that could not look must not
        get the permissive answer -- and the default must be the safe
        one, because every existing caller uses it."""
        for plan in (install_mod.plan_install(self._record('6.0.230'),
                                              '6.0.223',
                                              interpreter_exists=None),
                     install_mod.plan_install(self._record('6.0.230'),
                                              '6.0.223')):
            self.assertEqual(plan['action'], 'refuse_downgrade')

    def test_a_dead_interpreter_does_not_authorise_taking_over(self):
        """An external supervisor owns start/stop. A missing interpreter
        is not permission to rewrite someone else's definition.

        OURS OLDER, deliberately. The first draft of this test used the
        SAME version on both sides, so `ours < theirs` was False and the
        branch it names was never entered -- it passed with the whole
        repair branch deleted, and with it returning literal nonsense.
        It is the newer-external record that must not plan a repair,
        because repair_runtime refuses one anyway: without the guard the
        user is asked to confirm a repair, waits through a venv build,
        and is refused afterwards.
        """
        got = install_mod.plan_install(
            self._record('6.0.230', 'external'), '6.0.223',
            interpreter_exists=False)
        self.assertEqual(got['action'], 'refuse_downgrade')
        self.assertNotEqual(got['action'], 'repair_runtime')

    def test_an_external_supervisor_at_the_same_version_is_still_external(
            self):
        """The neighbouring cell of the same truth table: no downgrade in
        play, so the external opt-out answers as it always did."""
        got = install_mod.plan_install(
            self._record('6.0.223', 'external'), '6.0.223',
            interpreter_exists=False)
        self.assertEqual(got['action'], 'external')

    def test_a_dead_interpreter_does_not_authorise_an_UNREPAIRABLE_kind(
            self):
        """The neighbouring hole, and the same defect one kind over.
        `external` was excluded by name; every OTHER kind repair_runtime
        cannot re-register was still authorised -- so the plan offered a
        repair, ConvoyExt asked the user to confirm it, spent minutes
        building a venv, and repair_runtime then answered
        'unknown_supervisor'. Ask-then-refuse, moved past consent.

        `none` is the reachable one (plan_install reports an empty field
        as exactly that string), and an unknown kind stands for whatever
        a newer Embody writes next.
        """
        for kind in ('none', 'systemd', 'owlette', 'launchd'):
            got = install_mod.plan_install(
                self._record('6.0.230', kind), '6.0.223',
                interpreter_exists=False)
            self.assertEqual(
                got['action'], 'refuse_downgrade',
                'supervisor %r was offered a repair repair_runtime '
                'refuses' % (kind,))

    def test_the_repairable_kinds_still_get_their_repair(self):
        """The guard must not close the door it was written to open.
        Both kinds repair_runtime handles, plus a record with NO
        supervisor field -- which repair_runtime defaults to the
        platform's own kind, so there IS a definition to write."""
        for record in ({'version': '6.0.230', 'supervisor': 'scheduled_task'},
                       {'version': '6.0.230', 'supervisor': 'launch_agent'},
                       {'version': '6.0.230'}):
            got = install_mod.plan_install(record, '6.0.223',
                                           interpreter_exists=False)
            self.assertEqual(got['action'], 'repair_runtime',
                             'record %r lost its repair' % (record,))

    def test_the_plan_and_the_repair_ask_the_SAME_question(self):
        """They agreed by coincidence before, in two places, on one kind.
        The point of b73fcd0's fix is that they cannot drift -- so they
        read one predicate, and this asserts they still do rather than
        that they currently happen to match."""
        for kind in ('scheduled_task', 'launch_agent', '', None,
                     'none', 'external', 'systemd', 'nonsense'):
            planned = install_mod.plan_install(
                {'version': '6.0.230', 'supervisor': kind}, '6.0.223',
                interpreter_exists=False)['action'] == 'repair_runtime'
            # 'external' needs no special case: it is not a kind the
            # predicate accepts either, so the two answers coincide
            # whichever branch actually refuses it.
            accepted = install_mod._supervisor_is_repairable(kind)
            self.assertEqual(
                planned, accepted,
                'plan_install and repair_runtime disagree about %r'
                % (kind,))

    def test_interpreter_exists_is_keyword_only(self):
        """ARITY GUARD. plan_install's `platform` parameter is passed
        POSITIONALLY by ConvoyExt and is unread in the body, so a new
        third positional would silently bind 'win32' to this boolean and
        every branch would flip with the tests still green."""
        params = inspect.signature(install_mod.plan_install).parameters
        self.assertEqual(params['interpreter_exists'].kind,
                         inspect.Parameter.KEYWORD_ONLY)
        with self.assertRaises(TypeError):
            install_mod.plan_install(self._record('6.0.230'), '6.0.223',
                                     'win32', False)

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


class TestConvoyRecordedInterpreterIsWindowless(EmbodyTestCase):
    """installed.json says WHICH SUBSYSTEM the recorded interpreter is.

    'An empty console window appears at every logon' was, until this
    field existed, answerable only from the user's desktop: the record
    named a path ending in pythonw.exe and nothing checked what that file
    actually was. Stamped in the ONE place both install() and
    repair_runtime() share, so a repair can never leave a stale claim.
    """

    def _verifier(self, data_dir, interpreter, platform=None,
                  architecture=None, runner=None):
        """A venv runtime: a live crypto pass with no managed bundle."""
        return {'ok': True, 'probe': {'python': [3, 11, 15],
                                      'cryptography_version': '49.0.0'}}

    def _install(self, root, interpreter, platform='win32'):
        got = install_mod.install(
            root, '6.0.171', _MODULES, interpreter, platform=platform,
            runner=_Runner(), env=WIN_ENV, home=root,
            runtime_verifier=self._verifier)
        self.assertTrue(got['ok'], got)
        return install_mod.read_installed(root, platform)

    def _exe(self, root, name, payload):
        return _write(os.path.join(root, 'runtime-venv', 'Scripts', name),
                      payload)

    def test_a_windowless_interpreter_is_recorded_as_gui(self):
        with _TempDir() as root:
            record = self._install(
                root, self._exe(root, 'pythonw.exe', _gui_pe()))
            self.assertEqual(record['interpreter_subsystem'], 'gui')
            self.assertIs(record['venv_runtime'], True,
                          'venv_runtime stays a BARE bool -- the launcher '
                          'reads it as one')

    def test_a_console_interpreter_is_recorded_as_console(self):
        """The whole defect, made visible in the record: a file NAMED
        pythonw.exe that is a console binary."""
        with _TempDir() as root:
            record = self._install(
                root, self._exe(root, 'pythonw.exe', _console_pe()))
            self.assertEqual(record['interpreter_subsystem'], 'console')

    def test_an_unreadable_interpreter_is_recorded_as_unknown(self):
        """A path, not a promise: the recorded interpreter may be gone by
        the time anyone reads the record (that is exactly what
        repair_runtime exists for), and 'unknown' must not read as
        'console'. NOT WIN_PY -- on a dev box that one is a real
        TouchDesigner pythonw.exe, and the test would silently start
        asserting nothing."""
        with _TempDir() as root:
            record = self._install(
                root, os.path.join(root, 'gone', 'pythonw.exe'))
            self.assertEqual(record['interpreter_subsystem'], 'unknown')

    def test_a_repair_restamps_it_and_never_leaves_a_stale_claim(self):
        with _TempDir() as root:
            self._install(root, self._exe(root, 'pythonw.exe',
                                          _console_pe()))
            repaired = install_mod.repair_runtime(
                root, self._exe(root, 'fixed.exe', _gui_pe()),
                platform='win32', runner=_Runner(), env=WIN_ENV,
                runtime_verifier=self._verifier)
            self.assertTrue(repaired['ok'], repaired)
            record = install_mod.read_installed(root, 'win32')
            self.assertEqual(record['interpreter_subsystem'], 'gui')
            self.assertIs(record['venv_runtime'], True)

    def test_posix_records_no_subsystem_at_all(self):
        """There is no such thing to get wrong on macOS, and a field that
        always says 'unknown' there would read as a fault."""
        with _TempDir() as root:
            record = self._install(root, MAC_PY, platform='darwin')
            self.assertNotIn('interpreter_subsystem', record)


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

    def test_a_dead_python_under_an_external_supervisor_says_so(self):
        """Embody must not rewrite another supervisor's definition, so
        pointing that user at Embody's own Repair button would be the
        same 'names a button that refuses' lie in a different place --
        and plan_install answers external/refuse there, never repair."""
        record = dict(self.RECORD, supervisor='external')
        got = install_mod.host_state(record, 'absent', None, '6.0.171',
                                     False)
        self.assertEqual(got['state'], 'needs_repair_python')
        self.assertIn('another supervisor', got['detail'])
        self.assertNotIn('Install re-resolves it', got['detail'])

    def test_a_dead_python_under_OUR_supervisor_names_install(self):
        """The sibling half: here Install really does re-resolve it."""
        got = install_mod.host_state(self.RECORD, 'absent', None,
                                     '6.0.171', False)
        self.assertEqual(got['state'], 'needs_repair_python')
        self.assertIn('Install re-resolves it', got['detail'])

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

class TestConvoyRepairRuntime(EmbodyTestCase):
    """Re-point an existing install at a new interpreter, changing nothing
    else.

    THE DEAD END THIS EXISTS FOR: the recorded Python is gone, so
    host_state says 'Needs repair -- Python not found (reinstall)', but
    the record was written by a NEWER Embody, so plan_install answers
    refuse_downgrade and InstallHost overwrites the warning with
    'installed by a newer Embody'. The machine's daemon stays dead with
    no route back through the UI.

    The invariant every test here defends is A-36: the newer host app
    must still be the code that comes back up.
    """

    NEWER = '6.0.230'
    OURS = '6.0.223'

    def _installed(self, root, runner=None):
        """A real install by a NEWER Embody, on disk."""
        got = install_mod.install(
            root, self.NEWER, _MODULES, WIN_PY, platform='win32',
            runner=runner or _Runner(), env=WIN_ENV,
            runtime_verifier=_approved_runtime)
        self.assertTrue(got['ok'], got)
        return install_mod.read_installed(root, 'win32')

    def _repair(self, root, interpreter, **kw):
        kw.setdefault('platform', 'win32')
        kw.setdefault('runner', _Runner())
        kw.setdefault('env', WIN_ENV)
        kw.setdefault('runtime_verifier', _approved_runtime)
        return install_mod.repair_runtime(root, interpreter, **kw)

    # -- the A-36 invariant ---------------------------------------------

    def test_the_installed_version_and_payload_are_untouched(self):
        with _TempDir() as root:
            before = self._installed(root)
            payload = install_mod.app_dir(root, self.NEWER, 'win32')
            names_before = sorted(os.listdir(payload))
            got = self._repair(root, r'C:\new\pythonw.exe')
            self.assertTrue(got['ok'], got)
            after = install_mod.read_installed(root, 'win32')
            self.assertEqual(after['version'], self.NEWER,
                             'a runtime repair must NEVER change the '
                             'installed version -- that is the downgrade '
                             'A-36 forbids')
            self.assertEqual(after['files'], before['files'])
            self.assertEqual(sorted(os.listdir(payload)), names_before,
                             'no payload may be written by a repair')

    def test_a_downgrade_is_unrepresentable_in_the_signature(self):
        """The strongest guarantee available: not 'untested', but
        impossible to ask for. repair_runtime takes no version and no
        modules, so no caller can express one."""
        params = inspect.signature(install_mod.repair_runtime).parameters
        self.assertNotIn('version', params)
        self.assertNotIn('modules', params)

    def test_the_interpreter_is_what_changes(self):
        with _TempDir() as root:
            self._installed(root)
            got = self._repair(root, r'C:\new\pythonw.exe')
            self.assertTrue(got['ok'], got)
            record = install_mod.read_installed(root, 'win32')
            self.assertEqual(record['interpreter'], r'C:\new\pythonw.exe')
            self.assertIn('repaired_at', record)

    def test_the_supervisor_is_re_registered_at_the_new_interpreter(self):
        with _TempDir() as root:
            self._installed(root)
            runner = _Runner()
            got = self._repair(root, r'C:\new\pythonw.exe', runner=runner)
            self.assertTrue(got['ok'], got)
            self.assertTrue(got['registered'])
            self.assertEqual(runner.calls[0][:2], ['schtasks', '/Create'])
            with open(install_mod.task_xml_path(root, 'win32'), 'rb') as f:
                xml = f.read().decode('utf-16')
            self.assertIn(r'C:\new\pythonw.exe', xml)
            self.assertNotIn(WIN_PY, xml,
                             'the dead interpreter must be gone from the '
                             'task definition, not merely accompanied')

    # -- the refusals ----------------------------------------------------

    def test_an_external_supervisor_is_never_rewritten(self):
        """A-36's opt-out is not weaker on the repair path."""
        with _TempDir() as root:
            install_mod.install(
                root, self.NEWER, _MODULES, WIN_PY, platform='win32',
                runner=_Runner(), env=WIN_ENV,
                supervisor=install_mod.SUPERVISOR_EXTERNAL,
                runtime_verifier=_approved_runtime)
            runner = _Runner()
            got = self._repair(root, r'C:\new\pythonw.exe', runner=runner)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'external_supervisor')
            self.assertEqual(runner.calls, [],
                             'never poke another supervisor')
            self.assertEqual(
                install_mod.read_installed(root, 'win32')['interpreter'],
                WIN_PY, 'and never rewrite its record either')

    def test_a_refused_interpreter_writes_nothing(self):
        def refuse(data_dir, interpreter, platform=None, architecture=None,
                   runner=None):
            return {'ok': False, 'reason': 'runtime_crypto_broken',
                    'detail': 'cannot load cryptography'}

        with _TempDir() as root:
            self._installed(root)
            runner = _Runner()
            got = self._repair(root, r'C:\new\pythonw.exe',
                               runner=runner, runtime_verifier=refuse)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'runtime_crypto_broken')
            self.assertEqual(runner.calls, [])
            self.assertEqual(
                install_mod.read_installed(root, 'win32')['interpreter'],
                WIN_PY, 'a refused repair leaves the old record intact')

    def test_an_unknown_supervisor_kind_is_refused_not_silently_skipped(
            self):
        """A kind with no branch would fall through the shared registrar
        taking NO action, and repair would return ok=True having
        registered nothing -- success over a daemon still pointed at a
        dead Python. install() cannot write such a record, but repair
        reads records other Embodies wrote."""
        with _TempDir() as root:
            self._installed(root)
            record = install_mod.read_installed(root, 'win32')
            record['supervisor'] = 'systemd-ish-from-the-future'
            install_mod.write_installed(root, record, 'win32')
            runner = _Runner()
            got = self._repair(root, r'C:\new\pythonw.exe', runner=runner)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'unknown_supervisor')
            self.assertEqual(runner.calls, [])

    def test_the_launcher_path_is_ours_never_the_records(self):
        """installed.json is a file another Embody wrote. Letting it name
        the write target would let a foreign record decide where this
        process writes a launcher and what the supervisor then runs."""
        with _TempDir() as root:
            self._installed(root)
            record = install_mod.read_installed(root, 'win32')
            record['launcher'] = os.path.join(root, 'elsewhere',
                                              'evil_launch.py')
            install_mod.write_installed(root, record, 'win32')
            got = self._repair(root, r'C:\new\pythonw.exe')
            self.assertTrue(got['ok'], got)
            canonical = install_mod.launcher_path(root, 'win32')
            self.assertEqual(got['launcher'], canonical)
            self.assertFalse(os.path.exists(record['launcher']),
                             'nothing may be written at the path the '
                             'record named')
            with open(install_mod.task_xml_path(root, 'win32'), 'rb') as f:
                self.assertIn(canonical, f.read().decode('utf-16'))

    def test_the_LIVE_account_wins_over_the_recorded_one(self):
        """A repair is the 'something changed' path. Re-registering a
        logon task for a renamed account fails silently -- the worst
        shape -- so the live account is resolved exactly as install()
        resolves it, and the record is only a last resort."""
        with _TempDir() as root:
            self._installed(root)
            record = install_mod.read_installed(root, 'win32')
            record['account'] = r'OLDDOMAIN\renamed_away'
            install_mod.write_installed(root, record, 'win32')
            got = self._repair(root, r'C:\new\pythonw.exe',
                               env={'USERDOMAIN': 'TEC-B4A',
                                    'USERNAME': 'admin'})
            self.assertTrue(got['ok'], got)
            with open(install_mod.task_xml_path(root, 'win32'), 'rb') as f:
                xml = f.read().decode('utf-16')
            self.assertIn(WIN_USER, xml)
            self.assertNotIn('renamed_away', xml)

    def test_the_returned_version_is_the_installed_one(self):
        """ConvoyExt compares the restarted daemon's reported version
        against exactly this field to decide whether the payload is
        stale. Unpinned, that lie detector could be fed anything."""
        with _TempDir() as root:
            self._installed(root)
            got = self._repair(root, r'C:\new\pythonw.exe')
            self.assertEqual(got['version'], self.NEWER)

    def test_repairing_nothing_is_a_stated_refusal(self):
        with _TempDir() as root:
            got = self._repair(root, r'C:\new\pythonw.exe')
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'not_installed')

    def test_it_never_raises(self):
        for interpreter in ('', None, r'C:\new\pythonw.exe'):
            got = install_mod.repair_runtime(
                '/nonexistent/data/dir', interpreter, platform='win32',
                runner=_Runner(), env=WIN_ENV,
                runtime_verifier=_approved_runtime)
            self.assertIn('ok', got)
            self.assertFalse(got['ok'])

    # -- the launcher bet it deliberately does not take ------------------

    def test_an_existing_launcher_is_left_alone(self):
        """An OLDER Embody rewriting the launcher that drives a NEWER
        payload is a cross-version contract bet with no test behind it.
        The dead interpreter is not in the launcher."""
        with _TempDir() as root:
            self._installed(root)
            launcher = install_mod.launcher_path(root, 'win32')
            with open(launcher, 'w', encoding='utf-8') as f:
                f.write('# a NEWER launcher this project must not replace\n')
            self._repair(root, r'C:\new\pythonw.exe')
            with open(launcher, encoding='utf-8') as f:
                self.assertEqual(
                    f.read(),
                    '# a NEWER launcher this project must not replace\n')

    def test_a_MISSING_launcher_is_restored(self):
        """The one exception: absent is not a version conflict."""
        with _TempDir() as root:
            self._installed(root)
            launcher = install_mod.launcher_path(root, 'win32')
            os.remove(launcher)
            got = self._repair(root, r'C:\new\pythonw.exe')
            self.assertTrue(got['ok'], got)
            self.assertTrue(os.path.isfile(launcher))
            self.assertIn('launcher', got['steps'])

    def test_the_runtime_shape_may_change_and_never_doubles_up(self):
        """A repair can move a venv install onto a managed runtime or the
        reverse; the two claims are mutually exclusive and the stale one
        must not survive."""
        def venv_verifier(data_dir, interpreter, platform=None,
                          architecture=None, runner=None):
            return {'ok': True, 'probe': {'python': [3, 11, 15]}}

        with _TempDir() as root:
            self._installed(root)          # managed: has a 'runtime' key
            self.assertIn('runtime',
                          install_mod.read_installed(root, 'win32'))
            got = self._repair(root, r'C:\new\pythonw.exe',
                               runtime_verifier=venv_verifier)
            self.assertTrue(got['ok'], got)
            record = install_mod.read_installed(root, 'win32')
            self.assertTrue(record.get('venv_runtime'))
            self.assertNotIn('runtime', record,
                             'a venv repair must not keep a managed '
                             'receipt it did not earn')


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
