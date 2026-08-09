"""
Test suite: the Convoy host-install RUNTIME LADDER (ConvoyExt.py's
module-level worker bodies).

WHY THIS FILE EXISTS, SEPARATELY FROM test_convoy_host_install.py.
The ladder that decides WHICH interpreter the daemon runs under is the
highest-consequence branch in the install path -- it is what writes a
path into a machine-scoped installed.json and into the Windows Scheduled
Task's <Command>. Until now every test of it lived in
test_convoy_host_install.py, which needs a live TouchDesigner session and
is not in pytest.ini testpaths: `pytest` collects NOTHING from that file,
and running it explicitly reports every one of its tests SKIPPED. So the
Windows preference and the macOS no-regression guard had coverage on
exactly zero CI runners -- the same "it sat here and no runner could test
it" problem that moved daemon_venv_spec into convoy_install.py.

It does not need TouchDesigner. ConvoyExt.py imports only the standard
library at module level, and _host_install / _host_build_daemon_venv are
module-level worker bodies that touch no TD object BY CONTRACT (the
sibling suite pins that with a source regex). So they can be loaded off
disk and driven directly, on windows AND macos AND ubuntu runners, with
the platform passed in rather than inferred.

CONVENTIONS, matching test_convoy_install.py:
  - the installer is a stub that RECORDS argv and never spawns anything;
  - probe_runtime is scripted per interpreter, so no candidate binary is
    ever executed;
  - the one thing that touches the real filesystem is a temp directory
    standing in for the daemon venv, because the ladder legitimately asks
    the filesystem whether a venv is already built.
"""

import importlib.util
import os
import shutil
import sys
import tempfile

# unit_tests/ -> embody/ -> dev/ -> the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_CONVOY_DIR = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody', 'convoy')


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_CONVOY_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


install_mod = _load('convoy_install_for_ladder', 'convoy_install.py')
# A DISTINCT module name: inside TouchDesigner the real extension is
# already imported as ConvoyExt, and shadowing it would swap the live
# extension's module out from under the running COMP.
convoy_mod = _load('convoy_ext_for_ladder', 'ConvoyExt.py')

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

PROJECT_VENV = 'C:/fake/project/.venv/Scripts/pythonw.exe'
SYSTEM_PY = 'C:/fake/sys/python.exe'
WIN_BASE = 'C:/Program Files/Python311/python.exe'
MAC_VENV = '/fake/project/.venv/bin/python'
MAC_DAEMON = '/fake/EmbodyConvoy/runtime-venv/bin/python3'
UV = 'C:/fake/uv.exe'


class _Client:
    """Nothing running, nothing answering /health."""

    STATUS_RUNNING = 'running'

    def read_live_portfile(self, *a, **kw):
        return None

    def probe(self, *a, **kw):
        raise RuntimeError('no daemon in this harness')


class _Installer:
    """The real convoy_install with every spawning seam replaced."""

    def __init__(self, real, build):
        self._real = real
        self.calls = []
        self.probed = []
        self.outcomes = {}
        self.base_version = '3.11'
        self.venv_ok = True
        self.recorded = None
        self._build = build

    def __getattr__(self, name):
        return getattr(self._real, name)

    def probe_runtime(self, interp, platform=None, architecture=None,
                      runner=None):
        self.probed.append(interp)
        if interp in self.outcomes:
            return dict(self.outcomes[interp])
        return {'ok': False, 'reason': 'runtime_probe_failed',
                'detail': 'unscripted %s' % (interp,)}

    def run_command(self, argv, timeout_s=None, **kw):
        self.calls.append(list(argv))
        if argv and argv[0] in (WIN_BASE, '/opt/homebrew/bin/python3',
                                '/usr/local/bin/python3'):
            return 0, self.base_version + '\n', ''
        if argv[:2] == [UV, 'venv']:
            if not self.venv_ok:
                return 1, '', 'uv venv exploded'
            self._build()
            return 0, '', ''
        return 0, '', ''

    def install(self, data_dir, version, modules, interpreter, **kw):
        self.calls.append(['INSTALL', interpreter])
        self.recorded = interpreter
        return {'ok': True, 'version': version, 'registered': False,
                'supervisor': 'scheduled_task', 'steps': ['payload']}

    def repair_runtime(self, data_dir, interpreter, **kw):
        self.calls.append(['REPAIR', interpreter])
        self.recorded = interpreter
        return {'ok': True, 'version': '6.0.230', 'registered': False,
                'supervisor': 'scheduled_task', 'steps': ['task_xml']}

    def start(self, **kw):
        return {'ok': True, 'results': []}


class _LadderBase(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='embody-ladder-')
        self.venv_dir = os.path.join(self.tmp, 'runtime-venv')
        self.scripts = os.path.join(self.venv_dir, 'Scripts')
        self.console_py = os.path.join(self.scripts, 'python.exe')
        self.daemon_py = os.path.join(self.scripts, 'pythonw.exe')
        self.installer = _Installer(install_mod, self.buildVenv)

    def tearDown(self):
        # NOT addCleanup: it does not fire under TestRunnerExt.
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def buildVenv(self):
        """What a successful `uv venv` leaves behind on Windows."""
        os.makedirs(self.scripts, exist_ok=True)
        for path in (self.console_py, self.daemon_py):
            with open(path, 'wb') as f:
                f.write(b'not really python')

    def spec(self, **over):
        spec = {'dir': self.venv_dir, 'python': self.console_py,
                'daemon_python': self.daemon_py, 'bases': [WIN_BASE]}
        spec.update(over)
        return spec

    def ctx(self, **over):
        ctx = {'client': _Client(), 'installer': self.installer,
               'platform': 'win32', 'data_dir': 'C:/fake/EmbodyConvoy',
               'version': '6.0.171', 'home': 'C:/fake/home', 'uid': None,
               'installed_by': 'C:/fake/project (/embody/Embody)',
               'health_wait_s': 0.0, 'health_poll_s': 0.0,
               'venv_python': PROJECT_VENV,
               'runtime_candidates': [PROJECT_VENV],
               'uv': UV, 'venv_python_repair': PROJECT_VENV,
               'venv_crypto_deps': ['cryptography>=3.4'],
               'daemon_venv': self.spec()}
        ctx.update(over)
        return ctx

    def script(self, outcomes):
        self.installer.outcomes = dict(outcomes)

    def install(self, ctx=None, **kw):
        ctx = ctx or self.ctx()
        return convoy_mod._host_install(
            ctx, {'convoy_hostapp.py': 'x'}, ctx['venv_python'], None,
            venv_runtime=True, **kw)

    def uvArgs(self, marker):
        return [a for a in self.installer.calls if a[:2] == [UV, marker]]


class TestWindowsPrefersThePerUserVenv(_LadderBase):
    """One machine has one Convoy daemon; it must not be pinned to one
    project's directory.

    THE FIELD DEFECT, measured at 6.0.223: with an empty runtime catalog
    the daemon ran under the CALLING PROJECT'S .venv, and that path went
    into a machine-scoped installed.json and into the Scheduled Task's
    <Command>. Moving or deleting that one project stopped the machine's
    daemon at the next logon, silently. Every test here fails against
    the ladder that shipped, where the project venv probes clean on
    Windows and wins the first rung.
    """

    def test_the_daemon_venv_wins_even_though_the_project_venv_passes(self):
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}},
                     self.daemon_py: {'ok': True, 'probe': {}}})
        got = self.install()
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.installer.recorded, self.daemon_py)
        self.assertNotIn(PROJECT_VENV, self.installer.probed,
                         'a healthy per-user venv means the project venv '
                         'is not even a question')

    def test_uv_gets_python_exe_but_pythonw_exe_is_recorded(self):
        """The two exes of a Windows venv are not interchangeable. uv is
        driven over pipes and gets the console binary; the SUPERVISOR
        launches (and installed.json records) the windowless one, or the
        user gets a console window on their desktop all session.
        Recording the wrong half is worse than either: the launcher
        refuses to start unless the recorded path realpath-matches the
        running one, so the task simply retries every minute forever,
        with nothing on screen."""
        self.script({self.daemon_py: {'ok': True, 'probe': {}}})
        got = self.install()
        self.assertTrue(got['ok'], got)
        pip = self.uvArgs('pip')
        self.assertEqual(len(pip), 1)
        self.assertEqual(pip[0][-2:], ['--python', self.console_py])
        self.assertEqual(self.installer.recorded, self.daemon_py)

    def test_an_existing_venv_is_reused_not_rebuilt(self):
        self.buildVenv()
        self.script({self.daemon_py: {'ok': True, 'probe': {}}})
        got = self.install()
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.uvArgs('venv'), [])
        self.assertEqual(self.installer.recorded, self.daemon_py)

    def test_a_broken_existing_venv_is_rebuilt_with_clear(self):
        self.buildVenv()
        queued = [{'ok': False, 'reason': 'runtime_probe_failed',
                   'detail': 'broken'}, {'ok': True, 'probe': {}}]

        def probe(interp, platform=None, architecture=None, runner=None):
            self.installer.probed.append(interp)
            if interp == self.daemon_py:
                return dict(queued.pop(0) if len(queued) > 1 else queued[0])
            return {'ok': False, 'reason': 'runtime_probe_failed',
                    'detail': 'unscripted'}

        self.installer.probe_runtime = probe
        got = self.install()
        self.assertTrue(got['ok'], got)
        self.assertEqual(len(self.uvArgs('venv')), 1)
        self.assertIn('--clear', self.uvArgs('venv')[0])
        self.assertEqual(self.installer.recorded, self.daemon_py)

    def test_it_is_never_probed_twice(self):
        """It is also in runtime_candidates -- that list still owns the
        FALLBACK order -- so the main loop must skip what the preference
        rung already tried. bases=[] so no rebuild can explain a second
        probe, and the project venv fails so the loop actually reaches
        the entry the guard exists to skip."""
        self.buildVenv()
        self.script({
            self.daemon_py: {'ok': False, 'reason': 'runtime_probe_failed',
                             'detail': 'broken'},
            PROJECT_VENV: {'ok': False, 'reason': 'runtime_probe_failed',
                           'detail': 'broken too'},
            SYSTEM_PY: {'ok': True, 'probe': {}}})
        got = self.install(self.ctx(
            daemon_venv=self.spec(bases=[]),
            runtime_candidates=[PROJECT_VENV, self.daemon_py, SYSTEM_PY]))
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.installer.probed.count(self.daemon_py), 1)

    def test_a_total_failure_still_builds_only_once(self):
        """The daemon-venv rung used to be LAST and is now FIRST on
        Windows. If the old rung is not suppressed, a machine where
        nothing works pays for two full builds before refusing."""
        self.script({
            self.daemon_py: {'ok': False, 'reason': 'runtime_probe_failed',
                             'detail': 'built but broken'},
            PROJECT_VENV: {'ok': False, 'reason': 'runtime_probe_failed',
                           'detail': 'broken'}})
        got = self.install()
        self.assertFalse(got['ok'])
        self.assertEqual(len(self.uvArgs('venv')), 1)


class TestThePreferenceIsNeverARequirement(_LadderBase):
    """Building needs uv, a 3.11+ base and a network for the
    cryptography wheel. A show LAN or a locked-down studio has none of
    those -- which is why the daemon is vendored rather than downloaded
    in the first place -- so a failed build must degrade to a working
    interpreter, never refuse the install."""

    def test_a_failed_build_falls_back_to_the_project_venv(self):
        self.installer.venv_ok = False
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}}})
        got = self.install()
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.installer.recorded, PROJECT_VENV)

    def test_no_uv_on_the_machine_still_installs(self):
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}}})
        got = self.install(self.ctx(uv=None))
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.installer.recorded, PROJECT_VENV)
        self.assertEqual(self.uvArgs('venv'), [])

    def test_an_old_base_python_is_gated_before_any_build(self):
        self.installer.base_version = '3.9'
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}}})
        got = self.install()
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.uvArgs('venv'), [],
                         'a 3.9 base must be rejected BEFORE the build')

    def test_no_base_python_names_python_org_never_brew(self):
        """Telling a Windows user to `brew install python` is worse than
        saying nothing: a confident instruction they cannot follow."""
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}}})
        got = self.install(self.ctx(daemon_venv=self.spec(bases=[])))
        self.assertTrue(got['ok'], got)
        self.assertIn('python.org', got['detail'])
        self.assertNotIn('brew install', got['detail'])


class TestTheLadderSaysWhatActuallyHappened(_LadderBase):
    """The cost of the fallback is invisible until the day it bites, so
    the outcome has to name it -- and must not name it when it did not
    happen. Both halves have been wrong here."""

    def test_a_project_venv_fallback_is_stated_on_SUCCESS(self):
        self.installer.venv_ok = False
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}}})
        got = self.install()
        self.assertIn('per-user Convoy venv could not be used',
                      got['detail'])
        self.assertIn('moved or deleted', got['detail'])

    def test_a_TOTAL_failure_never_claims_a_fallback_happened(self):
        """Nothing was installed on this path. Asserting that the daemon
        'falls back to this project venv' is a fresh lie in the error
        message that reports the failure."""
        self.script({
            self.daemon_py: {'ok': False, 'reason': 'runtime_probe_failed',
                             'detail': 'no'},
            PROJECT_VENV: {'ok': False, 'reason': 'runtime_probe_failed',
                           'detail': 'no'}})
        got = self.install()
        self.assertFalse(got['ok'])
        self.assertNotIn('moved or deleted', got['detail'])

    def test_a_SYSTEM_python_fallback_is_not_called_a_project_venv(self):
        """When the ladder lands on a system Python, the daemon is not
        project-scoped and saying so would be a new false statement in
        place of the one it replaced."""
        self.installer.venv_ok = False
        self.script({
            PROJECT_VENV: {'ok': False, 'reason': 'runtime_probe_failed',
                           'detail': 'no'},
            SYSTEM_PY: {'ok': True, 'probe': {}}})
        got = self.install(self.ctx(
            runtime_candidates=[PROJECT_VENV, SYSTEM_PY]))
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.installer.recorded, SYSTEM_PY)
        self.assertNotIn('moved or deleted', got['detail'])

    def test_a_failed_REPAIR_does_not_report_itself_as_an_install(self):
        self.script({
            self.daemon_py: {'ok': False, 'reason': 'runtime_probe_failed',
                             'detail': 'no'},
            PROJECT_VENV: {'ok': False, 'reason': 'runtime_probe_failed',
                           'detail': 'no'}})
        got = self.install(repair_only=True)
        self.assertFalse(got['ok'])
        self.assertEqual(got['action'], 'repair_runtime')

    def test_a_repair_re_points_without_installing(self):
        self.buildVenv()
        self.script({self.daemon_py: {'ok': True, 'probe': {}}})
        got = self.install(repair_only=True)
        self.assertTrue(got['ok'], got)
        self.assertEqual(got['action'], 'repair_runtime')
        self.assertIn(['REPAIR', self.daemon_py], self.installer.calls)
        self.assertNotIn('INSTALL',
                         [c[0] for c in self.installer.calls])
        self.assertIn('6.0.230', got['detail'],
                      'the INSTALLED version is what was re-pointed, not '
                      'this project version')
        self.assertNotIn('may be stale', got['detail'],
                         'comparing the daemon report against OUR version '
                         'would make the lie detector itself lie')


class TestMacOSOrderingIsUnchanged(_LadderBase):
    """The preference is Windows-only. On macOS the project venv is
    tried FIRST and the daemon venv stays the last resort -- the rung
    order a real Mac exercises and a Windows dev box cannot. Only the
    macos leg of the CI matrix ever sees this branch."""

    def macCtx(self, **over):
        ctx = self.ctx(platform='darwin', venv_python=MAC_VENV,
                       runtime_candidates=[MAC_VENV],
                       venv_python_repair=MAC_VENV,
                       daemon_venv={'dir': '/fake/EmbodyConvoy/runtime-venv',
                                    'python': MAC_DAEMON,
                                    'bases': ['/opt/homebrew/bin/python3']})
        ctx.update(over)
        return ctx

    def test_a_healthy_project_venv_wins_on_macos(self):
        self.script({MAC_VENV: {'ok': True, 'probe': {}}})
        got = self.install(self.macCtx())
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.installer.recorded, MAC_VENV)
        self.assertEqual(self.uvArgs('venv'), [],
                         'macOS must not build a daemon venv while the '
                         'project venv is healthy')

    def test_a_signature_blocked_venv_still_reaches_the_fallback(self):
        """The reason the macOS fallback exists: TouchDesigner's bundled
        python is signed with library validation and refuses every
        foreign-signed wheel when spawned standalone, so no reinstall
        can ever fix that interpreter."""
        self.script({
            MAC_VENV: {'ok': False,
                       'reason': 'runtime_crypto_signature_blocked',
                       'detail': 'different Team IDs'},
            MAC_DAEMON: {'ok': True, 'probe': {}}})
        got = self.install(self.macCtx())
        self.assertTrue(got['ok'], got)
        self.assertEqual(self.installer.recorded, MAC_DAEMON)
        self.assertEqual(len(self.uvArgs('venv')), 1)
        self.assertIn('/opt/homebrew/bin/python3', self.uvArgs('venv')[0])
