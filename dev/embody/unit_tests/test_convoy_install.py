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
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

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
                    'a/b', r'a\b', None, '  '):
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
        for escape in ('D:evil.py', 'C:foo', 'D:', 'Z:Users/evil.py',
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
            'user and restarts it within a minute if it stops. Loopback '
            'only; never elevated.</Description>\n'
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
        proc = subprocess.run([sys.executable, '-c', code],
                              capture_output=True, text=True, timeout=120)
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
        install_mod.write_payload(root, version, modules)
        if not complete:
            os.unlink(install_mod.complete_path(root, version))
        if record:
            install_mod.write_installed(root, {'version': version,
                                               'drain_interval': 2.0})
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
            for version in ('../elsewhere', '..\\elsewhere',
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


# -- 8. interpreter discovery + the drift canary -----------------------

class TestConvoyInstallInterpreterDiscovery(EmbodyTestCase):

    def _win_tree(self, root, builds=('2025.33070',), names=None):
        for build in builds:
            binary = os.path.join(root, 'TouchDesigner.%s' % build, 'bin')
            os.makedirs(binary, exist_ok=True)
            for name in (names or ('pythonw.exe', 'python.exe')):
                with open(os.path.join(binary, name), 'w') as f:
                    f.write('')
        return root

    def test_win32_discovery_finds_pythonw_and_python(self):
        with _TempDir() as root:
            self._win_tree(root)
            found = install_mod.find_interpreters('win32', roots=[root])
            self.assertEqual(len(found), 2)
            self.assertTrue(found[0]['windowless'])
            self.assertTrue(found[0]['path'].endswith('pythonw.exe'))
            self.assertEqual(found[0]['build'], (2025, 33070))

    def test_the_newest_build_wins(self):
        with _TempDir() as root:
            self._win_tree(root, builds=('2024.30000', '2025.33070',
                                         '2025.9000'))
            chosen = install_mod.choose_interpreter(
                install_mod.find_interpreters('win32', roots=[root]))
            self.assertIn('TouchDesigner.2025.33070', chosen)

    def test_pythonw_is_preferred_over_python(self):
        """A console python leaves a window on screen for as long as the
        daemon runs -- on a show machine that is not cosmetic."""
        with _TempDir() as root:
            self._win_tree(root)
            chosen = install_mod.choose_interpreter(
                install_mod.find_interpreters('win32', roots=[root]))
            self.assertTrue(chosen.endswith('pythonw.exe'))

    def test_python_exe_is_used_when_pythonw_is_absent(self):
        with _TempDir() as root:
            self._win_tree(root, names=('python.exe',))
            chosen = install_mod.choose_interpreter(
                install_mod.find_interpreters('win32', roots=[root]))
            self.assertTrue(chosen.endswith('python.exe'))

    def test_darwin_discovery_walks_the_bundle(self):
        """UNVERIFIED on hardware -- the bundle layout is from
        Derivative's documented structure, not from a Mac we have run
        this on. What IS proven is that a tree matching it is found."""
        with _TempDir() as root:
            binary = os.path.join(root, 'TouchDesigner.app', 'Contents',
                                  'Frameworks', 'Python.framework',
                                  'Versions', 'Current', 'bin')
            os.makedirs(binary)
            with open(os.path.join(binary, 'python3'), 'w') as f:
                f.write('')
            found = install_mod.find_interpreters('darwin', roots=[root])
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0]['path'].endswith('python3'))
            self.assertTrue(found[0]['windowless'])

    def test_an_empty_or_missing_root_yields_nothing_and_does_not_raise(self):
        with _TempDir() as root:
            self.assertEqual(
                install_mod.find_interpreters('win32', roots=[root]), [])
        self.assertEqual(
            install_mod.find_interpreters(
                'win32', roots=['/definitely/not/here']), [])
        self.assertIsNone(install_mod.choose_interpreter([]))
        self.assertIsNone(install_mod.choose_interpreter(None))

    def test_a_directory_with_no_interpreter_is_skipped(self):
        with _TempDir() as root:
            os.makedirs(os.path.join(root, 'TouchDesigner.2025.33070',
                                     'bin'))
            self.assertEqual(
                install_mod.find_interpreters('win32', roots=[root]), [])

    def test_win32_install_roots_have_not_drifted_from_the_bridge(self):
        """DRIFT CANARY. This module deliberately does NOT import the
        bridge (A-44 forbids it, and the bridge answers a different
        question -- which app can I LAUNCH, not which interpreter can I
        RUN A SCRIPT UNDER). The cost of that copy is exactly this test:
        if someone moves TD's install root for the bridge and not here,
        interpreter discovery silently finds nothing and every install
        reports 'Needs repair -- Python not found'."""
        spec = importlib.util.spec_from_file_location('_bridge_canary',
                                                      _BRIDGE_PATH)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        self.assertEqual(install_mod.TD_INSTALL_ROOTS['win32'],
                         bridge._td_install_roots('win32'),
                         'the bridge moved TD\'s Windows install root and '
                         'convoy_install did not follow')


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

    def test_install_writes_everything_and_registers_once(self):
        with _TempDir() as root:
            runner = _Runner()
            got = install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
                                      platform='win32', runner=runner)
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

    def test_installed_json_is_written_LAST_of_all(self):
        """It is what the launcher reads to find its payload. Written
        earlier, a crash mid-install would leave a record pointing at
        files that do not exist; written last, a crash leaves the
        PREVIOUS install intact and running."""
        with _TempDir() as root:
            runner = _Runner()
            got = install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
                                      platform='win32', runner=runner)
            self.assertEqual(got['steps'][-1], 'installed.json')
            self.assertEqual(got['steps'][0], 'payload')

    def test_a_failed_registration_leaves_no_install_record(self):
        """Half-installed must never read as installed."""
        with _TempDir() as root:
            runner = _Runner(returncode=1, stderr='ERROR: access denied')
            got = install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
                                      platform='win32', runner=runner)
            self.assertFalse(got['ok'])
            self.assertEqual(got['reason'], 'register_failed')
            self.assertIn('access denied', got['detail'])
            self.assertIsNone(install_mod.read_installed(root, 'win32'))

    def test_install_registers_the_task_for_THIS_account(self):
        """The XML actually written to disk must carry the UserId, in
        both places -- the defect measured 2026-08-01 was invisible
        until a real schtasks saw the file."""
        with _TempDir() as root:
            install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
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
            got = install_mod.install(
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
            got = install_mod.install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=_Runner(),
                                      home=os.path.join(root, 'home'),
                                      user=None)
            self.assertTrue(got['ok'], got)

    def test_install_records_the_interpreter_and_the_drain_interval(self):
        with _TempDir() as root:
            install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner(),
                                drain_interval=5.0,
                                installed_by='/project/x.toe')
            record = install_mod.read_installed(root, 'win32')
            self.assertEqual(record['interpreter'], WIN_PY)
            self.assertEqual(record['drain_interval'], 5.0)
            self.assertEqual(record['installed_by'], '/project/x.toe')
            self.assertEqual(record['supervisor'], 'scheduled_task')

    def test_an_external_supervisor_install_registers_NOTHING(self):
        """A-36: write the payload, never a second supervisor."""
        with _TempDir() as root:
            runner = _Runner()
            got = install_mod.install(
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
                got = install_mod.install(root, bad[0], bad[1], bad[2],
                                          platform='win32',
                                          runner=_Runner())
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
            runner = _Runner()
            got = install_mod.install(root, '6.0.171', _MODULES, MAC_PY,
                                      platform='darwin', runner=runner,
                                      home=home, uid=501)
            self.assertTrue(got['ok'], got)
            self.assertTrue(os.path.isfile(
                install_mod.plist_path(home, 'darwin')))
            self.assertEqual([c[1] for c in runner.calls],
                             ['enable', 'bootstrap'])
            self.assertIn('gui/501/tools.embody.convoy.host',
                          runner.calls[0])

    def test_a_windows_install_needs_no_enable_step(self):
        """schtasks /Create /F rewrites the whole definition including
        <Enabled>true</Enabled>, so registration already re-enables."""
        with _TempDir() as root:
            runner = _Runner()
            install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=runner,
                                user=WIN_USER)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][:2], ['schtasks', '/Create'])

    def test_a_darwin_install_without_a_home_is_refused(self):
        with _TempDir() as root:
            got = install_mod.install(root, '6.0.171', _MODULES, MAC_PY,
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
            install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
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
            install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
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
            install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner())
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
            install_mod.install(root, '6.0.171', _MODULES, WIN_PY,
                                platform='win32', runner=_Runner())
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
        allowed = {'json', 'ntpath', 'os', 'posixpath', 're',
                   'subprocess', 'sys', 'time', 'xml'}
        for module in _imported_modules(_module_ast()):
            self.assertIn(module.split('.')[0], allowed,
                          '%s is not in the stdlib allowlist' % module)

    def test_it_is_ascii_only_with_unix_newlines_and_no_bom(self):
        with open(_INSTALL_PATH, 'rb') as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'), 'no BOM')
        self.assertNotIn(b'\r\n', raw, 'LF only')
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
            # conftest is pytest's, and manual_exit_proof is a hand-run
            # harness -- neither is imported by the daemon.
            if name in ('conftest.py', 'manual_exit_proof.py'):
                continue
            on_disk.add(name)
        self.assertEqual(set(install_mod.HOST_MODULES), on_disk)
        # The count is asserted as well as the set so that a rename plus
        # an addition cannot cancel out. It moved 9 -> 10 when Phase 3
        # slice 1 added convoy_hostkeys.py, and THIS TEST is what caught
        # the payload that would otherwise have shipped without it.
        self.assertEqual(len(install_mod.HOST_MODULES), 10,
                         'nine plan modules plus convoy_hostkeys.py')

    def test_the_module_docstring_states_the_honest_limits(self):
        """The code must not read softer than the install dialog. If
        these sentences go, the docs and the dialog have drifted from
        what is actually true."""
        doc = install_mod.__doc__
        for claim in ('per-user', 'UNVERIFIED', 'persistence',
                      'signed', 'Loopback'):
            self.assertIn(claim, doc)
