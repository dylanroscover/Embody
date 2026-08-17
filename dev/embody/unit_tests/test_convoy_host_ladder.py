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


class TestTheInterpreterProbeIsSharedNotCopied(_LadderBase):
    """b73fcd0's fix is an AGREEMENT between two questions, and it was
    kept by having the same six lines written twice.

    host_state decides the readout ('Needs repair -- Python not found');
    plan_install decides the button (repair_runtime, or
    refuse_downgrade). They must answer the same question about the same
    recorded path -- that identity IS the fix -- and two verbatim copies
    are exactly how it drifts back apart.
    """

    def test_the_probe_answers_the_filesystem(self):
        with tempfile.TemporaryDirectory() as root:
            live = os.path.join(root, 'python.exe')
            with open(live, 'w', encoding='utf-8') as handle:
                handle.write('')
            self.assertIs(
                convoy_mod._host_recorded_interpreter_exists(
                    {'interpreter': live}), True)
            self.assertIs(
                convoy_mod._host_recorded_interpreter_exists(
                    {'interpreter': os.path.join(root, 'gone.exe')}), False)

    def test_an_unaskable_question_is_UNKNOWN_not_absent(self):
        """None, never False. plan_install's repair branch is strictly
        `is False` for this reason: an answer nobody could look up is
        not evidence, and treating it as 'the Python is gone' would
        authorise a repair off a failed stat."""
        for installed in (None, {}, {'interpreter': ''},
                          {'interpreter': None}):
            self.assertIsNone(
                convoy_mod._host_recorded_interpreter_exists(installed),
                'a record with no recorded interpreter must read UNKNOWN')

    def test_a_stat_that_raises_is_UNKNOWN_too(self):
        class Hostile(str):
            def __str__(self):
                raise OSError('the path itself is unusable')

        self.assertIsNone(convoy_mod._host_recorded_interpreter_exists(
            {'interpreter': Hostile('x')}))

    def test_the_probe_exists_exactly_ONCE_in_the_source(self):
        """The structural half. Both call sites must reach the helper,
        so `os.path.isfile` may appear against the recorded interpreter
        in one place only -- the helper's own body."""
        path = os.path.join(_CONVOY_DIR, 'ConvoyExt.py')
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        self.assertEqual(
            source.count('_host_recorded_interpreter_exists'), 3,
            'expected one definition and two call sites')
        self.assertEqual(
            source.count("interpreter_exists = os.path.isfile"), 0,
            'an inline copy of the probe is back in ConvoyExt.py')


class TestARepairDoesNotSayItIsInstalling(_LadderBase):
    """A runtime repair writes no payload and cannot change the
    installed version, so 'Installing...' over it states something that
    is not going to happen -- on the one path the user reached BECAUSE
    the version may not be replaced."""

    def test_the_extension_mirrors_the_new_transient_state(self):
        self.assertEqual(convoy_mod.ConvoyExt.HOST_REPAIRING, 'repairing')

    def test_it_is_a_DIFFERENT_state_from_installing(self):
        self.assertNotEqual(convoy_mod.ConvoyExt.HOST_REPAIRING,
                            convoy_mod.ConvoyExt.HOST_INSTALLING)

    def test_the_install_button_picks_the_note_from_repair_only(self):
        """Read InstallHost itself: the note handed to _beginHostCall is
        chosen by `repair_only`, not fixed at HOST_INSTALLING."""
        import inspect
        source = inspect.getsource(convoy_mod.ConvoyExt.InstallHost)
        self.assertIn('self.HOST_REPAIRING if repair_only', source)
        self.assertIn('else self.HOST_INSTALLING', source)


class TestEnablingWaitsForTheSharedEnvironment(EmbodyTestCase):
    """A fresh install enables Envoy and Convoy seconds apart.

    THE FIELD FAILURE THIS PINS (2026-08-09, clean v6.0.230 install on a
    clean machine): the wizard enabled Envoy, which starts building its
    venv on a background worker that runs for MINUTES, then enabled Convoy
    a second later. Convoy's enable path went straight to InstallHost,
    found no interpreter -- because the venv did not exist YET -- failed
    the install outright, and told the user to "Enable Envoy first", which
    is precisely what they had just done.

    Neither the smoke nor any unit test could see it: the smoke asserted
    nothing about Convoy at all, and the ladder tests below drive the
    install with a runtime already resolved. So the question this class
    asks is the one nothing asked: MAY the install proceed at all yet?
    """

    class _Installer:
        """Records what was asked; answers the interpreter question only."""

        def __init__(self, managed=None, explode=False):
            self._managed = managed
            self._explode = explode

        def find_interpreters(self, platform):
            if self._explode:
                raise RuntimeError('probe blew up')
            return [self._managed] if self._managed else []

        def choose_interpreter(self, found):
            return found[0] if found else None

    def _resolvable(self, managed=None, venv_python=None, explode=False):
        ctx = {'installer': self._Installer(managed, explode),
               'platform': 'win32', 'venv_python': venv_python}
        # A @staticmethod, so it is called with the argument it actually
        # reads and nothing else -- no live COMP, and no fake `self` passed
        # positionally to make an unbound call type-check. That is the whole
        # reason this predicate has CI coverage rather than
        # TouchDesigner-only coverage.
        return convoy_mod.ConvoyExt._hostRuntimeResolvable(ctx)

    def test_no_runtime_yet_is_not_a_failure(self):
        """Envoy still building its venv: nothing to install UNDER yet."""
        self.assertFalse(self._resolvable(managed=None, venv_python=None),
                         'an install must not be attempted with no runtime')

    def test_the_project_venv_is_enough(self):
        self.assertTrue(self._resolvable(managed=None,
                                         venv_python=PROJECT_VENV))

    def test_a_managed_runtime_is_enough_without_any_venv(self):
        self.assertTrue(self._resolvable(managed=SYSTEM_PY, venv_python=None))

    def test_a_broken_probe_reads_as_not_ready_not_as_ready(self):
        """Fail SAFE: an exception must never be read as 'go ahead'."""
        self.assertFalse(self._resolvable(managed=SYSTEM_PY, explode=True))

    def test_enabling_asks_before_it_installs(self):
        """The ORDERING, pinned at the source.

        _ensureHostApp must consult the predicate before InstallHost --
        that is the entire fix. A source check rather than a behavioural
        one because the method needs a live COMP; without it this class
        would pass while the enable path still raced the venv.
        """
        path = os.path.join(_CONVOY_DIR, 'ConvoyExt.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        body = src[src.index('def _ensureHostApp'):]
        body = body[:body.index('\n    def ', 10)]
        self.assertIn('_hostRuntimeResolvable', body,
                      'enabling Convoy must check for a runtime BEFORE '
                      'attempting the install')
        self.assertLess(
            body.index('_hostRuntimeResolvable'), body.index('InstallHost'),
            'the runtime check must come BEFORE InstallHost, or the install '
            'still races the venv Envoy is building')
        self.assertIn('_awaitHostRuntime', body,
                      'a missing runtime must schedule a retry, not give up')


class TestABlockedSpawnIsNotABadInterpreter(EmbodyTestCase):
    """A probe that never reached Python must not be read as "bad Python".

    FIELD EVIDENCE (2026-08-09): a TouchDesigner started by the Envoy
    bridge inherits a NUL stdin, and EVERY subprocess it attempts dies
    with `OSError: [WinError 50] The request is not supported` -- proven
    by spawning `python -c "print(1)"` from such a session and watching
    it fail identically for the system Python AND the Convoy runtime
    venv. Convoy reported that as "no interpreter on this machine could
    load cryptography and TLS 1.3 ... install Python from python.org",
    which sends the user to install a Python they already have and
    cannot possibly help, because the interpreter was never the problem.
    """

    def test_winerror_50_is_classified_as_a_blocked_spawn(self):
        self.assertEqual(
            install_mod.classify_probe_failure(
                'OSError: [WinError 50] The request is not supported'),
            'runtime_spawn_blocked')

    def test_an_invalid_handle_is_the_same_class(self):
        self.assertEqual(
            install_mod.classify_probe_failure(
                'OSError: [WinError 6] The handle is invalid'),
            'runtime_spawn_blocked')

    def test_a_real_interpreter_problem_is_still_reported_as_one(self):
        """The new class must not swallow the failures it sits beside."""
        self.assertEqual(
            install_mod.classify_probe_failure(
                "ModuleNotFoundError: No module named 'cryptography'"),
            'runtime_missing_cryptography')
        self.assertEqual(
            install_mod.classify_probe_failure(
                'ImportError: dlopen(...cryptography..._rust...): '
                'different Team IDs'),
            'runtime_crypto_signature_blocked')
        self.assertEqual(
            install_mod.classify_probe_failure('some other stderr'),
            'runtime_probe_failed')


class TestTheBlockedSpawnMessageIsSaidONCE(EmbodyTestCase):
    """Naming the cause is right; naming it twice in one line is not.

    When every interpreter candidate dies before Python runs, the install
    path ALREADY returns a detail that is the whole explanation -- "this
    TouchDesigner process cannot start ANY child process ... Quit
    TouchDesigner, open the project yourself, and enable Convoy again".
    Wrapping that in a second copy of the same paragraph logged the advice
    twice in one WARNING, ending in two different instructions.

    The three gates are asserted here rather than in a live session
    because the branch is inside _finishHost, which needs a COMP; the
    predicate itself needs nothing.
    """

    def _ask(self, action, result, text):
        # Unbound: _isBlockedSpawn reads self only to reach _installer(),
        # which this stand-in supplies. That keeps the whole decision
        # drivable without TouchDesigner.
        class _Self:
            _SPAWNING_HOST_ACTIONS = \
                convoy_mod.ConvoyExt._SPAWNING_HOST_ACTIONS

            def _installer(self):
                return install_mod

        return convoy_mod.ConvoyExt._isBlockedSpawn(_Self(), action,
                                                    result, text)

    _BLOCKED = 'OSError: [WinError 50] The request is not supported'

    def test_a_blocked_start_is_named(self):
        """The case the branch exists for: nothing else explains it."""
        self.assertTrue(self._ask('start', {'ok': False}, self._BLOCKED))

    def test_stop_and_uninstall_spawn_too(self):
        for action in ('stop', 'uninstall'):
            self.assertTrue(self._ask(action, {'ok': False}, self._BLOCKED),
                            action)

    def test_the_install_path_already_said_it(self):
        """probe_runtime returns reason='spawn_blocked' with a detail that
        IS this paragraph, so a second copy is pure duplication."""
        self.assertFalse(
            self._ask('install', {'ok': False, 'reason': 'spawn_blocked'},
                      'this TouchDesigner process cannot start ANY child '
                      'process, so no interpreter could be tested'))

    def test_an_install_that_failed_for_ANOTHER_reason_is_still_named(self):
        """The gate is the reason, not the action: an install whose own
        detail does NOT already explain a blocked spawn still needs it."""
        self.assertTrue(
            self._ask('install', {'ok': False, 'reason': 'no_usable_runtime'},
                      self._BLOCKED))

    def test_an_audit_is_never_told_it_cannot_launch_the_app(self):
        """preview and the forget-offline calls spawn NOTHING -- one is a
        plan, the others are daemon HTTP. Two of the four spawn markers
        ('WinError 6', 'The handle is invalid') are generic Windows handle
        errors that an HTTP call can raise for unrelated reasons, so an
        unscoped branch would tell a user to quit TouchDesigner over a
        socket error."""
        for action in ('preview', 'uninstall_preview', 'forget_offline',
                       'forget_offline_plan'):
            self.assertFalse(
                self._ask(action, {'ok': False},
                          'OSError: [WinError 6] The handle is invalid'),
                action)

    def test_an_ordinary_failure_gets_the_ordinary_line(self):
        self.assertFalse(self._ask('start', {'ok': False},
                                   'the daemon refused: port in use'))

    def test_the_marker_list_is_ASKED_never_copied(self):
        """A second copy here had already drifted to two of the four
        markers within one release. Every marker convoy_install knows must
        reach this decision, including the ones the old copy lost."""
        for text in ('OSError: [WinError 50] The request is not supported',
                     'OSError: [WinError 6] The handle is invalid'):
            self.assertTrue(self._ask('start', {'ok': False}, text), text)
        # And no SECOND COPY of the list: a marker used in a membership
        # test inside ConvoyExt is the drift starting over. Prose that
        # merely mentions WinError 50 is fine and is why this is an AST
        # check rather than a substring one.
        import ast
        source = open(os.path.join(_CONVOY_DIR, 'ConvoyExt.py'),
                      encoding='utf-8').read()
        markers = set(install_mod._SPAWN_FAILURE_MARKERS)
        copied = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Compare):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Constant)
                        and sub.value in markers):
                    copied.append((sub.lineno, sub.value))
        self.assertEqual([], copied,
                         'ConvoyExt tests a spawn marker itself again -- '
                         'the list lives in convoy_install and is reached '
                         'through is_spawn_failure(): %r' % (copied,))
        self.assertIn('is_spawn_failure', source)


class TestEnvoyEnabledButNotYetStarted(EmbodyTestCase):
    """"Enable Envoy" is the wrong thing to say to someone who just did.

    THE FIELD SEQUENCE (2026-08-09): the wizard sets Convoyenable BEFORE
    it enables Envoy, and Envoy's Start -- the only thing that sets its
    `_bootstrapping` flag -- is deferred 30 frames by parexec. So attempt
    0 of Convoy's runtime wait ALWAYS lands in the window where Envoy is
    enabled and has not started, and a predicate that required
    `_bootstrapping` read that as "Envoy is off" and printed the one
    message the whole ladder was written to eliminate.
    """

    class _Par:
        def __init__(self, value):
            self._value = value

        def eval(self):
            return self._value

    class _Embody:
        def __init__(self, enabled):
            self.par = type('P', (), {})()
            self.par.Envoyenable = \
                TestEnvoyEnabledButNotYetStarted._Par(enabled)

    def _ask(self, embody):
        class _Self:
            pass
        me = _Self()
        me._embody = embody
        return convoy_mod.ConvoyExt._envoyIsBringingTheEnvironment(me)

    def test_enabled_but_not_bootstrapping_yet_still_counts_as_coming(self):
        """No `_bootstrapping` attribute at all: exactly the 30-frame
        window the wizard's own ordering guarantees."""
        self.assertTrue(self._ask(self._Embody(1)))

    def test_switched_off_is_the_one_case_that_needs_the_user(self):
        self.assertFalse(self._ask(self._Embody(0)))

    def test_an_unreadable_parameter_does_not_promise_an_environment(self):
        """Fail toward the actionable message rather than toward waiting
        five minutes for something that may never arrive."""
        class _Broken:
            @property
            def par(self):
                raise RuntimeError('no COMP')
        self.assertFalse(self._ask(_Broken()))


class TestARegistrationRevivesAStaleHostLine(EmbodyTestCase):
    """A completed registration disproves a line saying the app is down.

    THE FIELD FAILURE (2026-08-10..11): a spawn-blocked session latched
    'Install failed -- see log' while the smoke run installed and
    started the daemon out of band; the extension heartbeated THROUGH
    that daemon for 20 hours with the failure line still on the panel,
    because nothing ever re-asked. _reviveDisprovenHostLine is the
    re-ask -- and its stand-downs are as load-bearing as the revive:
    a show must not repaint, an in-flight host action owns the line,
    and evidence older than a completed Stop must not resurrect a
    daemon that was just stopped.
    """

    def _fake(self, line, seq=0, sent_seq=0, performing=False, busy=False,
              host_state=None):
        cls = convoy_mod.ConvoyExt

        class _Self:
            _DISPROVEN_HOST_TEXTS = cls._DISPROVEN_HOST_TEXTS
            _host_status_text = line
            _host_busy = busy
            _host_line_seq = seq

            def __init__(me):
                me.session = {'register_host_seq': sent_seq}
                if host_state is not None:
                    me.session['host_state'] = host_state
                me.published = []
                me.logged = []

            def _session(me):
                return me.session

            def _performing(me):
                return performing

            def _hostStatus(me, state):
                me.published.append(state)

            def _log(me, msg, level='INFO'):
                me.logged.append((level, msg))

        return _Self()

    def _revive(self, fake, result=None):
        class _Client:
            HOST_RUNNING = 'running'
        convoy_mod.ConvoyExt._reviveDisprovenHostLine(
            fake, result or {}, _Client)

    def test_a_latched_install_failed_is_replaced_with_running(self):
        fake = self._fake('Install failed -- see log',
                          host_state={'state': 'not_running',
                                      'installed_version': '6.0.229',
                                      'supervisor': 'scheduled_task',
                                      'live': False,
                                      'detail': 'stale words',
                                      'pid': 4242})
        self._revive(fake, {'host_app_version': '6.0.234'})
        self.assertEqual(len(fake.published), 1, 'one recomputed line')
        state = fake.published[0]
        self.assertEqual(state['state'], 'running')
        self.assertIs(state['live'], True,
                      'a register IS proof of liveness')
        self.assertEqual(state['installed_version'], '6.0.234')
        self.assertEqual(state['supervisor'], 'scheduled_task',
                         'identity facts survive')
        self.assertNotIn('pid', state, 'a register proves no pid')
        self.assertNotIn('detail', state,
                         'the detail described the disproven state')
        self.assertEqual(fake.session['host_state'], state,
                         '_restoreHostStatus must not resurrect the claim')
        self.assertTrue(fake.logged, 'the transition is logged')

    def test_lines_a_register_does_not_disprove_stay(self):
        for line in ('Installing...', 'Checking...',
                     'Installed -- starting...', 'Needs repair -- Python '
                     'not found (reinstall)',
                     'Managed by another supervisor', 'Running 6.0.234'):
            fake = self._fake(line)
            self._revive(fake)
            self.assertEqual(fake.published, [], line)

    def test_perform_mode_freezes_the_readout(self):
        fake = self._fake('Install failed -- see log', performing=True)
        self._revive(fake)
        self.assertEqual(fake.published, [])

    def test_an_in_flight_host_action_owns_the_line(self):
        fake = self._fake('Install failed -- see log', busy=True)
        self._revive(fake)
        self.assertEqual(fake.published, [])

    def test_evidence_older_than_a_completed_stop_stands_down(self):
        """The register was SENT before the Stop finished writing
        'Installed -- stopped'; its success cannot prove the daemon
        survived that Stop, so reviving would be the 20-hour latch
        inverted."""
        fake = self._fake('Installed -- stopped', seq=7, sent_seq=6)
        self._revive(fake)
        self.assertEqual(fake.published, [])
        self.assertNotIn('host_state', fake.session,
                         'the snapshot is not rewritten either')


# ---------------------------------------------------------------------
# INSTALL VERIFICATION: settle -> one automatic restart -> visible state
# ---------------------------------------------------------------------


class _Handle:
    host_id = 'h' * 32


class _Probe:
    def __init__(self, live):
        self.use_convoy = bool(live)
        self.handle = _Handle() if live else None
        self.status = 'running' if live else 'absent'


class _VersionClient:
    """A daemon that answers /status with a SCRIPTED version per read.

    `versions` is consumed one entry per /status read and the last entry
    repeats, so a restart race is expressed directly: ['6.0.241', '6.0.246']
    means 'the outgoing payload answered first, then the new one'. A
    /shutdown also advances the script, because the next process to answer
    is one launched AFTER installed.json was rewritten -- which is exactly
    the mechanism the automatic repair relies on.
    """

    STATUS_RUNNING = 'running'
    HOST_STALE_PAYLOAD = 'stale_payload'

    def __init__(self, versions, extra=None):
        self.versions = list(versions)
        self.extra = dict(extra or {})
        self.reads = 0
        self.shutdowns = 0
        self.live = True
        # False models a daemon that never comes back at all -- the
        # 'unverified, not stale' case, where nothing may be claimed.
        self.revive_on_start = True

    def _version(self):
        if not self.versions:
            return None
        return self.versions[min(self.reads, len(self.versions) - 1)]

    def data_dir(self):
        return 'C:/fake/EmbodyConvoy'

    def read_live_portfile(self, *a, **kw):
        return {'pid': 4242} if self.live else None

    def probe(self, *a, **kw):
        return _Probe(self.live)

    def host_get(self, handle, path):
        if path != '/status':
            return 404, {}
        body = {'ok': True, 'app_version': self._version()}
        body.update(self.extra)
        self.reads += 1
        return 200, body

    def host_post(self, handle, path, body):
        if path == '/shutdown':
            self.shutdowns += 1
            self.reads += 1
            # The daemon really exits, so the portfile observer reports it
            # gone and convoy_install._await_exit returns at once. Without
            # this the observer would say 'still running' forever and the
            # test would burn the full EXIT_WAIT_S of REAL time -- the
            # wall-clock dependency this repo's CI rule forbids.
            self.live = False
            return 200, {'ok': True}
        return 404, {}


class _RegisteringInstaller(_Installer):
    """_Installer, but its install REGISTERS -- so _host_install runs the
    started/healthy/verify tail that the plain ladder tests skip."""

    def __init__(self, real, build, version='6.0.246'):
        super().__init__(real, build)
        self.version = version
        self.starts = 0
        self.start_kwargs = []
        self.client = None   # set by the test so start() can revive it

    def install(self, data_dir, version, modules, interpreter, **kw):
        self.calls.append(['INSTALL', interpreter])
        self.recorded = interpreter
        return {'ok': True, 'version': self.version, 'registered': True,
                'supervisor': 'scheduled_task', 'steps': ['payload']}

    def read_installed(self, data_dir, platform=None):
        """The record a real install has just written. _host_snapshot reads
        it to re-derive the stale-payload state on EVERY refresh, so the
        harness has to carry it or that derivation cannot happen."""
        # A REAL interpreter path: host_state answers 'needs repair -- Python
        # not found' when the recorded one is missing, which would mask the
        # state under test.
        return {'version': self.version, 'supervisor': 'scheduled_task',
                'interpreter': sys.executable,
                'modules': ['convoy_hostapp.py']}

    def start(self, **kw):
        self.starts += 1
        self.start_kwargs.append(dict(kw))
        if self.client is not None and self.client.revive_on_start:
            # The supervisor brings a daemon back up, so /health and /status
            # answer again -- and the one that comes back reads the record
            # written since, which is what the scripted version list models.
            self.client.live = True
        return {'ok': True, 'results': []}


class TestInstallVersionVerification(_LadderBase):
    """The install's lie detector, after the field report of 2026-08-16.

    Three consecutive updates (6.0.239, 6.0.241, 6.0.246) each logged 'the
    restarted daemon reports <the previous version> ... the payload it runs
    may be stale', and each time the payload was fine: the daemon answering
    was a launchd respawn of the OUTGOING process, because the LaunchAgent
    carries RunAtLoad+KeepAlive and install() writes installed.json last.
    One immediate read turned a transitional second into a permanent
    verdict -- printed only to the textport, telling the user to press a
    button they would never see.
    """

    def setUp(self):
        super().setUp()
        self.installer = _RegisteringInstaller(install_mod, self.buildVenv)

    def _ctx(self, versions, extra=None, **over):
        self.client = _VersionClient(versions, extra=extra)
        self.installer.client = self.client
        base = {'client': self.client, 'installer': self.installer,
                'health_wait_s': 0.0, 'health_poll_s': 0.0,
                'version_settle_attempts': 4, 'version_settle_s': 0.0,
                # Same version as the record, so host_state reads RUNNING
                # rather than 'installed by a newer Embody' -- the stale
                # derivation only ever speaks over a running daemon.
                'version': self.installer.version}
        base.update(over)
        return self.ctx(**base)

    def _install(self, versions, extra=None, **over):
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}},
                     self.daemon_py: {'ok': True, 'probe': {}}})
        return self.install(ctx=self._ctx(versions, extra=extra, **over))

    def test_a_matching_daemon_verifies_with_no_restart(self):
        got = self._install(['6.0.246'])
        self.assertTrue(got['ok'], got)
        self.assertIs(got['version_verified'], True)
        self.assertIsNone(got['restart_retry'],
                          'a daemon already on the new payload is left alone')
        self.assertEqual(self.client.shutdowns, 0)
        self.assertNotIn('stale', got['detail'])

    def test_the_restart_race_settles_instead_of_crying_wolf(self):
        """THE FIELD CASE. The outgoing payload answers first; waiting was
        all it took. The shipped code declared a stale payload here."""
        got = self._install(['6.0.241', '6.0.241', '6.0.246'])
        self.assertIs(got['version_verified'], True,
                      'a version that converges is verified, not stale')
        self.assertIsNone(got['restart_retry'],
                          'settling must not spend the repair')
        self.assertEqual(self.client.shutdowns, 0,
                         'nothing is restarted over a race that resolves')
        self.assertNotIn('WARNING', got['detail'])

    def test_a_stuck_daemon_is_repaired_automatically(self):
        """The reporter's question, answered in code: the installer performs
        the repair its own warning used to ask them to perform."""
        got = self._install(['6.0.241'] * 4 + ['6.0.246'])
        self.assertEqual(self.client.shutdowns, 1,
                         'exactly one automatic restart, never a loop')
        self.assertEqual(self.installer.starts, 2,
                         'the install start, plus the repair start')
        self.assertIs(got['version_verified'], True)
        self.assertTrue(got['restart_retry'],
                        'a self-heal must be recorded, never silent')
        self.assertIn('restarted once', got['detail'])

    def test_a_daemon_that_never_updates_is_reported_as_needing_repair(self):
        """And it reaches the READOUT, not just the textport -- that a
        textport WARNING is invisible was the whole complaint."""
        got = self._install(['6.0.241'])
        self.assertIs(got['version_verified'], False)
        self.assertEqual(self.client.shutdowns, 1,
                         'the repair is attempted exactly once')
        self.assertEqual(got['state']['state'], 'stale_payload')
        self.assertEqual(got['state']['reported_version'], '6.0.241')
        self.assertIn('6.0.241', got['detail'])
        self.assertIn('Repair Convoy App', got['detail'])

    def test_a_busy_daemon_is_never_restarted_under_its_own_work(self):
        """An out-of-date daemon serving jobs correctly is a smaller problem
        than one killed halfway through a dispatch."""
        got = self._install(['6.0.241'],
                            extra={'jobs_running': 1, 'polls_in_flight': 0})
        self.assertEqual(self.client.shutdowns, 0,
                         'work in flight outranks a version number')
        self.assertIs(got['version_verified'], False)
        self.assertEqual(got['restart_retry'], {'skipped': 'daemon busy'})
        self.assertIn('busy', got['detail'])

    def test_a_silent_daemon_is_unverified_not_stale(self):
        """None is not False. Nothing answered, so nothing is claimed, and
        the readout is NOT rewritten to needs-repair on no evidence."""
        self.script({PROJECT_VENV: {'ok': True, 'probe': {}},
                     self.daemon_py: {'ok': True, 'probe': {}}})
        ctx = self._ctx(['6.0.246'])
        self.client.live = False
        self.client.revive_on_start = False
        got = self.install(ctx=ctx)
        self.assertIsNone(got['version_verified'])
        self.assertNotEqual(got['state'].get('state'), 'stale_payload')
        self.assertIn('unverified', got['detail'])

    def test_settle_uses_the_injected_interval_not_a_real_wait(self):
        """CI runners stall; a real-clock settle would be a flake factory."""
        slept = []
        reported, ok, _body = convoy_mod._host_settle_version(
            self._ctx(['6.0.241']), '6.0.246', attempts=3,
            sleep=slept.append)
        self.assertFalse(ok)
        self.assertEqual(reported, '6.0.241')
        self.assertEqual(slept, [0.0, 0.0],
                         'one sleep between attempts, none after the last')

    def test_a_versionless_daemon_is_stale_not_silent(self):
        """A daemon that ANSWERS but names no version is a pre-6.0.213
        payload -- the strongest staleness signal there is, since it
        predates version reporting entirely. Folding it in with 'nobody
        answered' both mislabels it and skips the automatic repair it most
        needs (panel finding, 2026-08-16)."""
        got = self._install([None])
        self.assertIs(got['version_verified'], False,
                      'answered-without-a-version is a mismatch, not silence')
        self.assertEqual(self.client.shutdowns, 1,
                         'and it earns the automatic restart')
        self.assertNotIn('did not answer', got['detail'],
                         'a daemon that answered four times must never be '
                         'reported as unreachable')

    def test_the_stale_state_is_re_derived_from_a_plain_snapshot(self):
        """THE READOUT MUST SURVIVE A REFRESH. Stamped once by the install,
        it was erased by the very next status refresh -- which fires on
        every project save -- leaving the log line as the only trace, i.e.
        the exact invisibility the field report complained about."""
        self._install(['6.0.241'])
        state = convoy_mod._host_snapshot(self._ctxSameClient())
        self.assertEqual(state['state'], 'stale_payload',
                         'a refresh must re-derive it, never clear it')
        self.assertEqual(state['reported_version'], '6.0.241')

    def test_a_converged_daemon_stops_being_reported_as_stale(self):
        """Derived, not latched: the moment the daemon serves the installed
        version the readout goes back to running, with no repair needed."""
        self._install(['6.0.246'])
        state = convoy_mod._host_snapshot(self._ctxSameClient())
        self.assertNotEqual(state['state'], 'stale_payload')

    def test_a_source_daemon_is_never_called_stale(self):
        """A dev checkout's daemon reports 'source' -- unorderable, and a
        false 'Needs repair' on a healthy dev machine would be its own
        defect."""
        self._install(['source'])
        state = convoy_mod._host_snapshot(self._ctxSameClient())
        self.assertNotEqual(state['state'], 'stale_payload')

    def test_a_newer_daemon_is_never_called_stale(self):
        """Newer-than-the-record has its own, more specific words."""
        self._install(['6.0.999'])
        state = convoy_mod._host_snapshot(self._ctxSameClient())
        self.assertNotEqual(state['state'], 'stale_payload')

    def _ctxSameClient(self):
        """A fresh status-refresh ctx over the SAME daemon, as a Refresh
        pulse would build it."""
        return self.ctx(client=self.client, installer=self.installer,
                        health_wait_s=0.0, health_poll_s=0.0,
                        version_settle_attempts=1, version_settle_s=0.0,
                        version=self.installer.version)

    def test_the_automatic_restart_is_graceful_on_darwin(self):
        """THE PLATFORM THAT HAS THE BUG, pinned where CI can see it.

        The stale-payload race is macOS-specific: the LaunchAgent carries
        RunAtLoad+KeepAlive, so a respawn during the install window serves
        the outgoing payload. This Windows dev box cannot reproduce that
        (schtasks /Create registers without starting), and the hardware
        Mac is not a routine signal -- so the darwin restart is pinned by
        the commands it ISSUES, on every leg of the matrix.

        Graceful, never a hard kill: the daemon is ASKED to exit
        (/shutdown), waited for, and only then started. `launchctl
        kickstart -k` would SIGKILL it and leave a portfile naming a dead
        pid, which convoy_install documents as its own hazard.
        """
        self.script({MAC_VENV: {'ok': True, 'probe': {}},
                     MAC_DAEMON: {'ok': True, 'probe': {}}})
        ctx = self._ctx(['6.0.241'], platform='darwin', uid='501',
                        venv_python=MAC_VENV,
                        runtime_candidates=[MAC_VENV],
                        daemon_venv=self.spec(python=MAC_DAEMON,
                                              daemon_python=MAC_DAEMON,
                                              bases=['/opt/homebrew/bin/'
                                                     'python3']))
        out = convoy_mod._host_restart_for_version(ctx)

        # 1. It ASKS the daemon to exit, and waits for it, before starting.
        self.assertEqual(self.client.shutdowns, 1,
                         'the daemon is asked to exit first')
        self.assertIs(out['exited'], True,
                      'and the exit is WAITED for -- starting over a live '
                      'process is how the stale payload survived')
        # 2. It starts through the SUPERVISOR, on this platform.
        self.assertEqual(self.installer.start_kwargs[-1].get('platform'),
                         'darwin')
        self.assertEqual(self.installer.starts, 1, 'exactly one start')
        # 3. And that supervisor primitive is graceful on darwin. Asserted
        #    against the real generator, so a future switch to the hard-kill
        #    form fails here instead of in the field.
        argv = install_mod.supervisor_argv('start', 'darwin', uid='501')
        self.assertEqual(argv[0], 'launchctl')
        self.assertIn('kickstart', argv)
        self.assertNotIn(
            '-k', argv,
            'kickstart -k SIGKILLs the daemon and strands a portfile naming '
            'a dead pid -- convoy_install\'s own documented hazard')

    def test_work_detection_reads_both_counters_and_defaults_to_free(self):
        has_work = convoy_mod._host_daemon_has_work
        self.assertTrue(has_work({'jobs_running': 2}))
        self.assertTrue(has_work({'polls_in_flight': 1}))
        self.assertFalse(has_work({'jobs_running': 0, 'polls_in_flight': 0}))
        self.assertFalse(has_work({}), 'absent data is not evidence of work')
        self.assertFalse(has_work(None))
        self.assertFalse(has_work({'jobs_running': 'lots'}),
                         'an unreadable counter must not block the repair')
