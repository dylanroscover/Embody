"""
Test suite: ConvoyExt's host-app orchestration (install plan STEP 4).

NORMAL TIER. Nothing here is destructive and nothing here may become
destructive: no test writes a payload, spawns schtasks or launchctl,
registers a Scheduled Task or LaunchAgent, opens a socket, starts a
thread, or reads -- let alone removes -- anything under the REAL
per-user Convoy data dir. That last one is the sharp edge. convoy_client
and convoy_install both default to the machine's live
%LOCALAPPDATA%\\EmbodyConvoy, so a single un-stubbed data_dir() here
would point a test at the host app the developer is actually running.
Every seam that could reach it is replaced:

  - ``_installer`` -> StubInstaller, which WRAPS the real convoy_install
    and replaces only the functions that touch the OS (read_installed,
    install/start/stop/uninstall, plan_host_uninstall, run_command,
    find_interpreters). Every pure decision function under test --
    plan_install, host_state, supervisor_argv, parse_supervisor_status
    -- is therefore the SHIPPING implementation, not a mock that could
    agree with a bug.
  - ``_client`` -> StubClient, wrapping the real convoy_client with
    data_dir / probe / read_live_portfile / host_post replaced.
    host_status_text is the shipping one, because the vocabulary is
    exactly what several of these tests assert.
  - ``_runInWorker`` -> synchronous, so a whole install round trip
    completes inside one test method with no thread and no timing race.
  - the module-level ``run()`` scheduler -> a recorder.
  - ``_hostStatus`` -> a recorder that still computes its text through
    the real host_status_text.
  - ``_dialog`` -> a seeded answer, so no modal ever opens.

Coverage:
  - THE VOCABULARY: every convoy_install.STATE_* constant maps to one of
    the twelve plan strings, all of them distinct, none of them the
    unrecognised-state default, and all of them ASCII.
  - Install: the happy path writes the payload and starts the
    supervisor; re-running over a broken install REPAIRS it rather than
    refusing; a downgrade is the one refusal; a missing payload, a
    missing interpreter, and a declined dialog all write NOTHING.
  - Stop: the supervisor is disabled, and disabled BEFORE it is ended.
  - Uninstall: a preview alters no state at all; the confirmation names
    what is kept; a cancel removes nothing; and the retained paths
    (host.json, host.token, host.portfile.json, audit.jsonl, jobs/) are
    never in any removal list, checked against the real
    plan_host_uninstall as well as against a poisoned plan.
  - Chain hygiene: a stale INSTANCE drops its host poll, a superseded
    generation retries instead of applying, and the cap reports honestly.
  - Threading: exactly one mod.convoy_install in the file, and every
    worker body is a module-level function that cannot reach `self`.
"""

import os
import re

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

FAKE_DATA_DIR = 'C:/fake/EmbodyConvoy'
FAKE_HOME = 'C:/fake/home'
FAKE_INTERPRETER = 'C:/fake/TouchDesigner.2025.33070/bin/pythonw.exe'
VERSION = '6.0.171'

# The twelve strings of plan section 1.2, verbatim and ASCII-only. This
# list is the contract: docs/convoy/index.md promises a table a user can
# look any observed string up in, so a string that is not here is a
# string nobody can look up.
VOCABULARY = (
    'Not installed',
    'Checking...',
    'Installing...',
    'Installed -- starting...',
    'Installed -- not running (restarts within a minute)',
    'Installed -- stopped',
    'Installed -- no supervisor (use Repair Convoy App)',
    'Needs repair -- Python not found (reinstall)',
    'Managed by another supervisor',
    'Install failed -- see log',
)


class StubClient:
    """The real convoy_client with every machine-reaching seam replaced."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.probe_status = real.STATUS_ABSENT
        self.portfile = None          # {'pid': .., 'port': ..} when live
        self.post_result = (200, {'ok': True, 'stopping': True})
        self.posted = []              # (path, body) of every host_post
        self.get_results = []         # queued (code, body) for host_get

    def __getattr__(self, name):
        return getattr(self._real, name)

    def data_dir(self, *a, **kw):
        # NEVER the real one. See the safety note in the module docstring.
        return FAKE_DATA_DIR

    def probe(self, *a, **kw):
        self.calls.append(('probe', None))
        if self.probe_status == self._real.STATUS_RUNNING:
            handle = self._real.HostHandle(port=41999, host_id='h' * 32,
                                           token='t' * 16,
                                           data_dir=FAKE_DATA_DIR)
            return self._real.ProbeResult(self._real.STATUS_RUNNING,
                                          handle=handle)
        return self._real.ProbeResult(self.probe_status, detail='stubbed')

    def read_live_portfile(self, *a, **kw):
        return dict(self.portfile) if self.portfile else None

    def host_post(self, handle, path, body, **kw):
        self.calls.append(('host_post', path))
        self.posted.append((path, dict(body or {})))
        result = self.post_result
        if callable(result):
            result = result(path, body)
        return result

    def host_get(self, handle, path, query=None, **kw):
        self.calls.append(('host_get', path))
        queue = self.get_results
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return 200, {'ok': True, 'host_id': 'h' * 32, 'nodes': []}

    def count(self, kind):
        return sum(1 for name, _ in self.calls if name == kind)


class StubInstaller:
    """The real convoy_install with every OS-mutating seam replaced.

    Constants, plan_install, host_state, supervisor_argv and
    parse_supervisor_status all fall through to the shipping module --
    they are decision logic, and stubbing decision logic is how a test
    comes to agree with a bug.
    """

    SCHTASKS_READY = (
        'Folder: \\\r\n'
        'HostName:                             TEC-B4A\r\n'
        'TaskName:                             \\EmbodyConvoyHost\r\n'
        'Status:                               Ready\r\n'
        'Scheduled Task State:                 Enabled\r\n')

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.installed = None            # what read_installed reports
        # A COMPLETE managed runtime: choose_interpreter now selects only
        # candidates carrying managed=True (the self-contained signed
        # CPython bundle), never TD's bundled Python or a system/venv
        # interpreter. find_interpreters is stubbed to return these, so a
        # missing managed flag here would make every install short-circuit
        # at the interpreter gate. The fail-closed "no managed runtime"
        # path is proven separately by
        # test_no_interpreter_refuses_before_the_dialog.
        self.interpreters = [{'path': FAKE_INTERPRETER,
                              'build': (2025, 33070), 'windowless': True,
                              'managed': True,
                              'runtime_id': 'rt_fake_managed'}]
        self.install_result = {'ok': True, 'version': VERSION,
                               'supervisor': 'scheduled_task',
                               'registered': True, 'steps': ['payload']}
        self.start_result = {'ok': True, 'results': []}
        self.stop_result = {'ok': True, 'results': [], 'exited': True}
        self.uninstall_result = {'ok': True, 'removed': [], 'kept': [],
                                 'remaining': [], 'complete': True}
        self.plan = None                 # plan_host_uninstall's answer

    def __getattr__(self, name):
        return getattr(self._real, name)

    # -- reads -------------------------------------------------------
    def read_installed(self, *a, **kw):
        return dict(self.installed) if self.installed else None

    def find_interpreters(self, *a, **kw):
        return [dict(c) for c in self.interpreters]

    def run_command(self, argv, **kw):
        # A LIST, always -- and never actually spawned.
        self.calls.append(('run_command', list(argv)))
        return 0, self.SCHTASKS_READY, ''

    def plan_host_uninstall(self, data_dir, platform=None, home=None):
        self.calls.append(('plan_host_uninstall', data_dir))
        if self.plan is not None:
            return dict(self.plan)
        join = self._real._join(platform)
        root = self._real.install_root(data_dir)
        retain = [join(root, n) for n in self._real.RETAINED_NAMES]
        retain.extend(join(root, n) for n in self._real.RETAINED_DIRS)
        return {'remove': [self._real.launcher_path(data_dir, platform),
                           self._real.installed_path(data_dir, platform)],
                'remove_dirs': [self._real.bin_dir(data_dir, platform)],
                'retain': retain, 'retain_present': retain,
                'incomplete': [], 'stray': [],
                'jobs': 7, 'indeterminate': 2}

    # -- writes (recorded, never performed) --------------------------
    def install(self, data_dir, version, modules, interpreter, **kw):
        # Exercise the graceful seams like the real install() does on a
        # repair-over-running: without callable shutdown/is_running the
        # darwin field failure (bootstrap EIO at a loaded label) comes
        # back with every test still green.
        shutdown = kw.get('shutdown')
        is_running = kw.get('is_running')
        observed_running = None
        if callable(is_running):
            observed_running = bool(is_running())
            if observed_running and callable(shutdown):
                shutdown()
        self.calls.append(('install', {'data_dir': data_dir,
                                       'version': version,
                                       'modules': sorted(modules),
                                       'interpreter': interpreter,
                                       'supervisor': kw.get('supervisor'),
                                       'graceful_seams': (
                                           callable(shutdown)
                                           and callable(is_running)),
                                       'observed_running': observed_running}))
        return dict(self.install_result)

    def start(self, **kw):
        self.calls.append(('start', dict(kw)))
        return dict(self.start_result)

    def stop(self, **kw):
        self.calls.append(('stop', dict(kw)))
        # Exercise the injected seams the way the real stop() does, so a
        # ConvoyExt that handed over a broken shutdown or observer fails
        # here instead of on a user's machine.
        shutdown = kw.get('shutdown')
        if shutdown is not None:
            self.calls.append(('shutdown', shutdown()))
        observer = kw.get('is_running')
        if observer is not None:
            self.calls.append(('is_running', observer()))
        return dict(self.stop_result)

    def uninstall(self, data_dir, **kw):
        self.calls.append(('uninstall', {'data_dir': data_dir}))
        shutdown = kw.get('shutdown')
        if shutdown is not None:
            self.calls.append(('shutdown', shutdown()))
        return dict(self.uninstall_result)

    # -- helpers ------------------------------------------------------
    def count(self, kind):
        return sum(1 for name, _ in self.calls if name == kind)

    def last(self, kind):
        for name, payload in reversed(self.calls):
            if name == kind:
                return payload
        return None

    def argvs(self):
        return [payload for name, payload in self.calls
                if name == 'run_command']


class _FakeDAT:
    """The two attributes _hostModuleName reads, and nothing else."""

    def __init__(self, name, file_value=None):
        self.name = name
        self.isDAT = True
        self.text = 'source of %s' % (name,)
        self.par = type('P', (), {})()
        if file_value is not None:
            self.par.file = type('F', (), {'eval': lambda s: file_value})()


def _convoy_comp(case):
    comp = case.embody.op('convoy')
    if comp is None:
        case.skipTest('the convoy child COMP is not in this project')
    return comp


def _modules(case, comp):
    client = comp.op('convoy_client')
    installer = comp.op('convoy_install')
    if client is None or installer is None:
        case.skipTest('convoy_client / convoy_install are not DATs in the '
                      'convoy COMP (the vendoring step has not landed)')
    return client.module, installer.module


class TestHostStatusVocabulary(EmbodyTestCase):
    """convoy_client.host_status_text is the SINGLE source of the words.

    Driven by convoy_install's OWN constants, not by a copied list: the
    two modules deliberately hold the state names twice (one owns the
    decision, one owns the vocabulary), and a rename on either side that
    this test did not catch would show up as a status field silently
    falling through to 'Install failed -- see log'.
    """

    def setUp(self):
        super().setUp()
        comp = _convoy_comp(self)
        self.client, self.install = _modules(self, comp)

    def _states(self):
        return {name: value for name, value in vars(self.install).items()
                if name.startswith('STATE_') and isinstance(value, str)}

    def test_every_installer_state_has_its_own_string(self):
        seen = {}
        for name, value in sorted(self._states().items()):
            text = self.client.host_status_text({'state': value,
                                                 'installed_version': VERSION,
                                                 'live': True, 'pid': 4242})
            self.assertNotEqual(
                text, 'Install failed -- see log',
                '%s (%r) fell through to the unrecognised-state default -- '
                'convoy_install and convoy_client have drifted'
                % (name, value))
            self.assertNotIn(
                text, seen,
                '%s and %s both read %r; two different host states that '
                'read the same are two states a user cannot tell apart'
                % (name, seen.get(text), text))
            seen[text] = name
        self.assertGreaterEqual(len(seen), 8)

    def test_the_transient_states_are_covered_too(self):
        comp = _convoy_comp(self)
        ext = comp.ext.ConvoyExt
        for name, expected in (('HOST_CHECKING', 'Checking...'),
                               ('HOST_INSTALLING', 'Installing...'),
                               ('HOST_STARTING', 'Installed -- starting...'),
                               ('HOST_INSTALL_FAILED',
                                'Install failed -- see log')):
            value = getattr(ext, name)
            self.assertEqual(value, getattr(self.client, name),
                             'ConvoyExt.%s must mirror convoy_client.%s'
                             % (name, name))
            self.assertEqual(self.client.host_status_text(value), expected)

    def test_the_exact_plan_strings(self):
        cases = (
            ({'state': self.install.STATE_NOT_INSTALLED}, 'Not installed'),
            ({'state': self.install.STATE_NOT_RUNNING},
             'Installed -- not running (restarts within a minute)'),
            ({'state': self.install.STATE_STOPPED}, 'Installed -- stopped'),
            ({'state': self.install.STATE_NO_SUPERVISOR},
             'Installed -- no supervisor (use Repair Convoy App)'),
            ({'state': self.install.STATE_NEEDS_REPAIR_PYTHON},
             'Needs repair -- Python not found (reinstall)'),
            ({'state': self.install.STATE_EXTERNAL_SUPERVISOR},
             'Managed by another supervisor'),
            ({'state': self.install.STATE_RUNNING,
              'installed_version': '6.0.171', 'pid': 4242},
             'Running 6.0.171 (pid 4242)'),
            ({'state': self.install.STATE_NEWER_INSTALL,
              'installed_version': '6.0.180', 'live': True},
             'Running 6.0.180 -- installed by a newer Embody'),
        )
        for state, expected in cases:
            self.assertEqual(self.client.host_status_text(state), expected)

    def test_running_never_claims_a_pid_it_does_not_have(self):
        text = self.client.host_status_text(
            {'state': self.install.STATE_RUNNING,
             'installed_version': '6.0.171'})
        self.assertEqual(text, 'Running 6.0.171')

    def test_a_silent_newer_install_does_not_say_running(self):
        # The one place the plan's literal wording is bent, and the
        # reason: printing 'Running' over a daemon that answered nothing
        # is exactly the failure this field exists to prevent.
        text = self.client.host_status_text(
            {'state': self.install.STATE_NEWER_INSTALL,
             'installed_version': '6.0.180', 'live': False})
        self.assertEqual(text,
                         'Installed 6.0.180 -- installed by a newer Embody')

    def test_the_vocabulary_is_total_and_never_raises(self):
        for junk in (None, '', 'nonsense', 42, [], {'state': None}):
            self.assertEqual(self.client.host_status_text(junk),
                             'Install failed -- see log')

    def test_every_string_is_ascii(self):
        # ascii-punctuation.md: '--' not an em dash, '...' not an
        # ellipsis. This string is written into a TD parameter a Windows
        # textport may render through a legacy codepage.
        states = list(self._states().values()) + [
            'checking', 'installing', 'starting', 'install_failed']
        for value in states:
            text = self.client.host_status_text(
                {'state': value, 'installed_version': VERSION, 'pid': 1})
            text.encode('ascii')
            for glyph in ('\u2014', '\u2013', '\u2026', '\u2019', '\u201c'):
                self.assertNotIn(glyph, text)

    def test_the_documented_vocabulary_is_reachable(self):
        """Every string the plan promises must be produced by SOME state.

        A string in the docs that no code path can emit is a lie in the
        troubleshooting table; a string the code emits that the docs do
        not list is one a user cannot look up. This pins the first.
        """
        produced = set()
        for value in list(self._states().values()) + [
                'checking', 'installing', 'starting', 'install_failed']:
            produced.add(self.client.host_status_text({'state': value}))
            produced.add(self.client.host_status_text(
                {'state': value, 'installed_version': VERSION, 'pid': 7,
                 'live': True}))
        for expected in VOCABULARY:
            self.assertIn(expected, produced,
                          '%r is in the documented vocabulary but no host '
                          'state produces it' % (expected,))


class ConvoyHostBase(EmbodyTestCase):
    """Shared fixture. No thread, no socket, no scheduler, no real files."""

    def setUp(self):
        super().setUp()
        self.comp = _convoy_comp(self)
        self.convoy = self.comp.ext.ConvoyExt
        self.convoy_mod = self.comp.op('ConvoyExt').module
        client_mod, install_mod = _modules(self, self.comp)
        self.install_mod = install_mod
        self.client = StubClient(client_mod)
        self.installer = StubInstaller(install_mod)

        self._patches = []
        self._runs = []
        self._logs = []
        self.host_states = []
        self.host_texts = []
        self.dialogs = []
        self.choice = 1
        self.session = {}

        self._patch(self.convoy, '_client', lambda: self.client)
        self._patch(self.convoy, '_installer', lambda: self.installer)
        self._patch(self.convoy, '_runInWorker',
                    lambda fn: (fn(), True)[1])
        self._patch(self.convoy_mod, 'run', self._fakeRun)
        self._patch(self.convoy, '_log',
                    lambda msg, level='INFO', details=None:
                        self._logs.append((msg, level)))
        self._patch(self.convoy, '_hostStatus', self._recordHostStatus)
        self._patch(self.convoy, '_dialog', self._fakeDialog)
        self._patch(self.convoy, '_session', lambda: self.session)
        self._patch(self.convoy, '_performing', lambda: False)
        self._patch(self.convoy, '_hostModules',
                    lambda: {'convoy_hostapp.py': 'x',
                             'convoy_platform.py': 'y'})
        self._patch(self.convoy, '_hostContext', self._fakeContext)
        self._patch(self.convoy, '_host_busy', False)
        self._patch(self.convoy, '_host_result', None)
        self._patch(self.convoy, '_host_gen', 0)
        # The REGISTRATION slot is patched too -- not because anything
        # here uses it, but so the separation test can write to it and
        # tearDown puts the live reconciler's bookkeeping back.
        self._patch(self.convoy, '_result', None)
        self._patch(self.convoy, '_busy', False)

    def tearDown(self):
        while self._patches:
            obj, name, old, had = self._patches.pop()
            if had:
                setattr(obj, name, old)
            else:
                try:
                    delattr(obj, name)
                except Exception:
                    pass
        super().tearDown()

    def _patch(self, obj, name, value):
        had = name in getattr(obj, '__dict__', {})
        old = obj.__dict__.get(name) if had else None
        setattr(obj, name, value)
        self._patches.append((obj, name, old, had))

    def _fakeContext(self):
        """The real _hostContext's shape, with no live values in it."""
        return {'client': self.client, 'installer': self.installer,
                'platform': 'win32', 'data_dir': FAKE_DATA_DIR,
                'version': VERSION, 'home': FAKE_HOME, 'uid': None,
                'installed_by': 'C:/fake/project (/embody/Embody)',
                'health_wait_s': 0.0, 'health_poll_s': 0.0}

    def _fakeRun(self, *a, **kw):
        """Record every scheduled call; DISPATCH only the initial poll."""
        self._runs.append((a, kw))
        if not (a and isinstance(a[0], str) and '_pollHostCall' in a[0]):
            return
        try:
            ext, action, gen, attempts = a[1], a[2], a[3], a[4]
        except IndexError:
            return
        if attempts == 0:
            ext._pollHostCall(action, gen, attempts)

    def _recordHostStatus(self, state):
        self.host_states.append(state)
        try:
            self.host_texts.append(self.client.host_status_text(state))
        except Exception as e:
            self.host_texts.append('<unreadable: %s>' % (e,))

    def _fakeDialog(self, title, message, buttons):
        self.dialogs.append((title, message, list(buttons)))
        return self.choice

    def _warnings(self):
        return [m for m, level in self._logs if level == 'WARNING']


class TestInstallOrchestration(ConvoyHostBase):

    def test_install_writes_the_payload_and_starts_the_supervisor(self):
        result = self.convoy.InstallHost()
        self.assertEqual(result['state'], 'installing')
        self.assertEqual(self.installer.count('install'), 1)
        sent = self.installer.last('install')
        self.assertEqual(sent['data_dir'], FAKE_DATA_DIR)
        self.assertEqual(sent['version'], VERSION)
        self.assertEqual(sent['interpreter'], FAKE_INTERPRETER)
        self.assertEqual(sent['modules'],
                         ['convoy_hostapp.py', 'convoy_platform.py'])
        self.assertEqual(self.installer.count('start'), 1,
                         'a registered supervisor is started immediately -- '
                         'otherwise the first launch waits for the next '
                         'repetition and Install reads as broken')
        self.assertTrue(sent['graceful_seams'],
                        'install must receive callable shutdown/is_running '
                        '-- without them a repair over a RUNNING daemon '
                        'bootstraps into a loaded label (macOS EIO 5, field '
                        'failure 2026-08-04) or silently leaves old code '
                        'running (Windows)')
        self.assertEqual(self.host_texts[0], 'Installing...')

    def test_install_asks_first_and_names_what_it_registers(self):
        self.convoy.InstallHost()
        self.assertLen(self.dialogs, 1)
        _title, message, buttons = self.dialogs[0]
        self.assertEqual(buttons[0], 'Cancel', 'the safe answer is button 0')
        # The dialog names the real consequences of the LAN-intended build: it
        # runs whenever logged in, opens an authenticated peer listener on the
        # trusted LAN (it does NOT expose Envoy), any same-user process can read
        # its token, it never asks for administrator, and it registers a
        # supervisor. It no longer claims loopback-only or "unsigned" -- the
        # runtime is a managed build whose signing status is in the release
        # notes (review 2026-08-03).
        for sentence in ('WHETHER OR NOT', 'trusted LAN', 'peer listener',
                         'never exposes Envoy', 'CAN READ', 'administrator',
                         'Scheduled Task', 'do NOT enable Convoy on a guest'):
            self.assertIn(sentence, message,
                          'the install dialog must state %r (plan 1.6)'
                          % (sentence,))
        self.assertIn(FAKE_INTERPRETER, message,
                      'the dialog must name the interpreter that will run '
                      'at login')
        self.assertIn(FAKE_DATA_DIR, message)

    def test_cancel_writes_nothing(self):
        self.choice = 0
        result = self.convoy.InstallHost()
        self.assertEqual(result['state'], 'declined')
        self.assertEqual(self.installer.count('install'), 0)
        self.assertEqual(self.installer.count('start'), 0)
        self.assertEqual(self.installer.argvs(), [],
                         'a declined install must not spawn a supervisor '
                         'command of any kind')
        self.assertEqual(self._warnings(), [],
                         'declining a system modification is not an error')

    def test_a_suppressed_dialog_is_a_decline(self):
        self.choice = -1
        self.assertEqual(self.convoy.InstallHost()['state'], 'declined')
        self.assertEqual(self.installer.count('install'), 0)

    def test_install_is_the_repair_path(self):
        """Re-running over a broken install must FIX it, not refuse."""
        self.installer.installed = {'version': VERSION,
                                    'supervisor': 'scheduled_task',
                                    'interpreter': 'C:/gone/pythonw.exe'}
        result = self.convoy.InstallHost()
        self.assertEqual(result['state'], 'installing')
        self.assertEqual(result['action'], self.install_mod.ACTION_CURRENT,
                         'the same version is a stated no-op to plan_install')
        self.assertEqual(
            self.installer.count('install'), 1,
            'and Install runs anyway -- rewriting the payload and '
            're-resolving the interpreter IS the repair for "Needs repair" '
            'and "no supervisor"')
        self.assertEqual(self.installer.last('install')['interpreter'],
                         FAKE_INTERPRETER,
                         'the interpreter is re-resolved, not reused from '
                         'the stale record')

    def test_a_newer_install_is_the_one_refusal(self):
        self.installer.installed = {'version': '6.0.180',
                                    'supervisor': 'scheduled_task'}
        result = self.convoy.InstallHost()
        self.assertEqual(result['state'], 'refused')
        self.assertEqual(self.installer.count('install'), 0)
        self.assertEqual(self.dialogs, [],
                         'a refusal must not ask the user to confirm it')
        self.assertEqual(self.host_texts[-1],
                         'Installed 6.0.180 -- installed by a newer Embody')
        self.assertLen(self._warnings(), 1)

    def test_an_external_supervisor_is_never_given_a_second_one(self):
        self.installer.installed = {'version': '6.0.100',
                                    'supervisor': 'external'}
        self.convoy.InstallHost()
        sent = self.installer.last('install')
        self.assertEqual(sent['supervisor'],
                         self.install_mod.SUPERVISOR_EXTERNAL,
                         'A-36: never two supervisors -- the kind must be '
                         'passed through, or install() defaults to a '
                         'Scheduled Task and registers a second one')

    def test_an_unusable_own_version_is_not_reported_as_a_newer_install(self):
        """plan_install answers refuse_downgrade for TWO different things.

        'this Embody has no usable version to install' and 'a newer
        Embody owns it' arrive with the same action AND the same
        installed_version, so keying the readout off installed_version
        told a user with a broken version par to go hunting for a host
        app that was not the problem. Caught by exercising the branch.
        """
        self.installer.installed = {'version': '6.0.100'}
        ctx = self._fakeContext()
        ctx['version'] = '../evil'
        self._patch(self.convoy, '_hostContext', lambda: ctx)
        result = self.convoy.InstallHost()
        self.assertEqual(result['state'], 'refused')
        self.assertEqual(self.host_texts[-1], 'Install failed -- see log')
        self.assertEqual(self.installer.count('install'), 0)

    def test_no_vendored_modules_refuses_loudly(self):
        self._patch(self.convoy, '_hostModules', lambda: {})
        result = self.convoy.InstallHost()
        self.assertEqual(result['state'], 'error')
        self.assertEqual(self.installer.count('install'), 0)
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.host_texts[-1], 'Install failed -- see log')
        self.assertLen(self._warnings(), 1)

    def test_no_interpreter_refuses_before_the_dialog(self):
        self.installer.interpreters = []
        result = self.convoy.InstallHost()
        self.assertEqual(result['state'], 'error')
        self.assertEqual(self.dialogs, [],
                         'a machine with no managed Convoy runtime must refuse '
                         'BEFORE asking the user to grant anything')
        self.assertEqual(self.installer.count('install'), 0)

    def test_perform_mode_does_nothing(self):
        self._patch(self.convoy, '_performing', lambda: True)
        self.assertEqual(self.convoy.InstallHost()['state'], 'deferred')
        self.assertEqual(self.installer.calls, [])
        self.assertEqual(self.dialogs, [])

    def test_a_second_pulse_while_busy_is_ignored(self):
        self._patch(self.convoy, '_host_busy', True)
        self.assertEqual(self.convoy.InstallHost()['state'], 'deferred')
        self.assertEqual(self.installer.calls, [])


class TestStartStopOrchestration(ConvoyHostBase):

    def test_start_refuses_when_nothing_is_installed(self):
        result = self.convoy.StartHost()
        self.assertEqual(result['state'], 'not_installed')
        self.assertEqual(self.installer.count('start'), 0)
        self.assertEqual(self.host_texts[-1], 'Not installed')

    def test_start_runs_the_supervisor_and_reports_what_it_sees(self):
        self.installer.installed = {'version': VERSION,
                                    'supervisor': 'scheduled_task',
                                    'interpreter': FAKE_INTERPRETER}
        self.client.probe_status = self.client.STATUS_RUNNING
        self.client.portfile = {'pid': 4242, 'port': 41999}
        self.convoy.StartHost()
        self.assertEqual(self.installer.count('start'), 1)
        self.assertEqual(self.host_texts[0], 'Installed -- starting...')
        # host_state is the SHIPPING computation; needs_repair_python
        # outranks running, so the fake interpreter path must not exist
        # for this to read Running.
        self.assertStartsWith(self.host_texts[-1], 'Needs repair')

    def test_stop_goes_through_the_installer_that_disables_first(self):
        self.convoy.StopHost()
        self.assertEqual(self.installer.count('stop'), 1,
                         'StopHost must never kill the daemon directly -- '
                         'without the disable inside stop() the supervisor '
                         'respawns it within a minute and the button looks '
                         'broken')
        sent = self.installer.last('stop')
        self.assertTrue(callable(sent.get('shutdown')),
                        'the graceful POST /shutdown is injected by us: it '
                        'needs a live probe result and the per-install token')
        self.assertTrue(callable(sent.get('is_running')),
                        'and the liveness observer, or stop() cannot wait '
                        'for the daemon to actually be gone')

    def test_the_injected_shutdown_posts_to_a_confirmed_live_host(self):
        self.client.probe_status = self.client.STATUS_RUNNING
        self.convoy.StopHost()
        self.assertEqual(self.client.count('host_post'), 1)
        self.assertEqual(self.installer.last('shutdown')['ok'], True)

    def test_the_shutdown_is_a_stated_no_op_when_nothing_answers(self):
        self.client.probe_status = self.client.STATUS_ABSENT
        self.convoy.StopHost()
        self.assertEqual(self.client.count('host_post'), 0,
                         'no token may be sent to a host app that did not '
                         'confirm its identity on /health')
        self.assertFalse(self.installer.last('shutdown')['ok'])
        self.assertEqual(self.installer.count('stop'), 1,
                         'and the supervisor stop still runs -- an '
                         'unresponsive daemon is exactly why it exists')

    def test_the_liveness_observer_reads_through_the_pid_check(self):
        self.client.portfile = None
        self.convoy.StopHost()
        self.assertFalse(self.installer.last('is_running'))


class TestUninstallSafety(ConvoyHostBase):
    """A-41: uninstall is never an evidence-destruction path."""

    def test_preview_alters_nothing_at_all(self):
        result = self.convoy.PreviewHostUninstall()
        self.assertEqual(result['state'], 'previewing')
        self.assertEqual(self.installer.count('uninstall'), 0)
        self.assertEqual(self.installer.count('plan_host_uninstall'), 1)
        self.assertEqual(self.dialogs, [],
                         'an audit must never prompt')
        self.assertEqual(self.host_states, [],
                         'an audit must never move the readout either -- '
                         'the Convoy Host field is state')
        self.assertIsNotNone(self.session.get('uninstall_preview'))

    def test_uninstall_previews_then_confirms_then_removes(self):
        self.convoy.UninstallHost()
        self.assertLen(self.dialogs, 1)
        _title, message, buttons = self.dialogs[0]
        self.assertEqual(buttons[0], 'Cancel')
        self.assertIn('7 job records (2 indeterminate)', message,
                      'the confirmation must COUNT what is retained')
        self.assertIn('host.json', message,
                      'and NAME the retained paths')
        self.assertEqual(self.installer.count('uninstall'), 1)

    def test_cancelling_the_uninstall_removes_nothing(self):
        self.choice = 0
        self.convoy.UninstallHost()
        self.assertLen(self.dialogs, 1)
        self.assertEqual(self.installer.count('uninstall'), 0)
        self.assertEqual(self._warnings(), [])

    def test_the_real_plan_never_targets_a_retained_path(self):
        """Against the SHIPPING plan_host_uninstall, not the stub.

        The guard and the planner have to agree, and the only way to know
        they do is to run the guard over what the planner really returns
        -- including its retained DIRECTORY (jobs/), which no exact-match
        check would protect.
        """
        plan = self.install_mod.plan_host_uninstall(
            FAKE_DATA_DIR, 'win32', FAKE_HOME)
        self.assertEqual(self.convoy._uninstallTargetsRetained(plan), [])
        for name in self.install_mod.RETAINED_NAMES:
            targets = [p for p in plan['remove'] + plan['remove_dirs']
                       if p.replace('\\', '/').endswith('/' + name)]
            self.assertEqual(
                targets, [],
                '%s must never appear in a removal list' % (name,))

    def test_a_plan_that_aims_at_retained_state_is_refused(self):
        root = self.install_mod.install_root(FAKE_DATA_DIR)
        poisoned = self.installer.plan_host_uninstall(FAKE_DATA_DIR, 'win32')
        poisoned['remove'] = list(poisoned['remove']) + [
            self.install_mod._join('win32')(root, 'host.json')]
        self.installer.plan = poisoned
        self.convoy.UninstallHost()
        self.assertEqual(self.installer.count('uninstall'), 0,
                         'a plan that would delete host.json must be refused '
                         'outright, not shown to the user to click past')
        self.assertEqual(self.dialogs, [])
        self.assertLen(self._warnings(), 1)

    def test_a_removal_inside_the_retained_jobs_directory_is_refused(self):
        join = self.install_mod._join('win32')
        root = self.install_mod.install_root(FAKE_DATA_DIR)
        poisoned = self.installer.plan_host_uninstall(FAKE_DATA_DIR, 'win32')
        poisoned['remove'] = list(poisoned['remove']) + [
            join(join(root, 'jobs'), 'job_abc.json')]
        self.installer.plan = poisoned
        self.convoy.UninstallHost()
        self.assertEqual(self.installer.count('uninstall'), 0,
                         'jobs/ is retained as a DIRECTORY -- a target '
                         'underneath it destroys the same evidence without '
                         'ever equalling the retained path')
        self.assertLen(self._warnings(), 1)

    def test_a_missing_preview_is_refused_rather_than_guessed(self):
        self.convoy._confirmUninstall(None)
        self.assertEqual(self.installer.count('uninstall'), 0)
        self.assertEqual(self.dialogs, [])


class TestHostChainHygiene(ConvoyHostBase):
    """The generation-tagged handoff, on its own slot."""

    def test_a_stale_instance_drops_its_poll_and_clears_its_slot(self):
        """A stale poll applies nothing and reschedules nothing -- but it
        DOES clear its own slot. The flags live on the same object the
        staleness check guards, so a check that ever misfires on the
        live instance would otherwise orphan the busy flag forever (the
        Mac first-install wedge, 2026-08-04). A genuinely superseded
        object's slot is unreachable garbage; clearing it harms nobody."""
        self._patch(self.convoy, '_staleInstance', lambda: True)
        self.convoy._host_busy = True
        self.convoy._host_result = {'_gen': 7, '_action': 'status',
                                    'result': {'ok': True,
                                               'state': {'state': 'running'}}}
        self.convoy._pollHostCall('status', 7, 0)
        self.assertEqual(self.host_states, [],
                         'a superseded instance must apply nothing')
        self.assertEqual(self._runs, [], 'and must not reschedule')
        self.assertIsNone(self.convoy._host_result,
                          'and must clear its own slot')
        self.assertFalse(self.convoy._host_busy,
                         'and must never leave its busy flag orphaned')

    def test_a_superseded_generation_retries_instead_of_applying(self):
        self.convoy._host_result = {'_gen': 4, '_action': 'status',
                                    'result': {'ok': True}}
        self.convoy._pollHostCall('status', 5, 0)
        self.assertEqual(self.host_states, [],
                         'an older generation must never be applied')
        self.assertLen(self._runs, 1, 'the poll re-arms instead')

    def test_the_poll_gives_up_with_an_honest_status(self):
        self.convoy._pollHostCall('install', 5, self.convoy.HOST_POLL_ATTEMPTS)
        self.assertEqual(self.host_texts[-1], 'Install failed -- see log')
        self.assertFalse(self.convoy._host_busy)
        self.assertLen(self._warnings(), 1)

    def test_the_host_slot_is_separate_from_the_registration_slot(self):
        """A 20 s install and a 30 s heartbeat must not drain each other."""
        self.convoy._result = {'_gen': 99, '_action': 'register'}
        self.convoy._host_result = None
        self.convoy.HostStatus()
        self.assertEqual(self.convoy._result,
                         {'_gen': 99, '_action': 'register'},
                         'the host chain must never touch _result')

    def test_a_worker_exception_still_publishes_a_result(self):
        def _boom():
            raise RuntimeError('worker exploded')
        self.convoy._beginHostCall('status', _boom)
        self.assertFalse(self.convoy._host_busy,
                         'the poll must drain, not spin to its cap')
        self.assertLen(self._warnings(), 1)

    def test_checking_never_flashes_over_a_known_state(self):
        self.convoy.HostStatus()
        self.assertEqual(self.host_texts[0], 'Checking...')
        settled = len(self.host_texts)
        self.convoy.HostStatus()
        self.assertNotIn('Checking...', self.host_texts[settled:],
                         'flashing Checking... over a good "Running X "'
                         '(pid N)" on every refresh is pure flicker')

    def test_host_status_snapshot_is_total(self):
        snap = self.convoy.HostStatus()
        for key in ('state', 'installed_version', 'supervisor', 'live',
                    'pid', 'detail', 'busy', 'status'):
            self.assertDictHasKey(snap, key)
        self.assertNotIn('error', snap, 'the snapshot must never raise')


class TestHostModuleReading(ConvoyHostBase):
    """The payload is lifted off the DATs on the MAIN THREAD."""

    def test_the_externalized_file_par_names_the_payload_entry(self):
        dat = _FakeDAT('convoy_hostapp',
                       'embody/Embody/convoy/host/convoy_hostapp.py')
        self.assertEqual(self.convoy._hostModuleName(dat),
                         'convoy_hostapp.py')

    def test_a_windows_file_par_still_yields_a_bare_name(self):
        dat = _FakeDAT(
            'convoy_platform',
            r'C:\repo\dev\embody\host\convoy_platform.py')
        self.assertEqual(self.convoy._hostModuleName(dat),
                         'convoy_platform.py')

    def test_an_unexternalized_dat_falls_back_to_its_name(self):
        self.assertEqual(self.convoy._hostModuleName(_FakeDAT('convoy_peers')),
                         'convoy_peers.py')

    def test_anything_that_is_not_a_bare_py_filename_is_skipped(self):
        # 'D:evil.py' is the one convoy_install calls out by name: no
        # separator, no dot-dot, not absolute -- and ntpath.join still
        # resolves it against drive D:'s own current directory.
        for value in ('D:evil.py', 'notpython.txt', '.py', 'con voy.py'):
            self.assertEqual(
                self.convoy._hostModuleName(_FakeDAT('x', value)), '',
                'write_payload refuses %r; guessing a correction would '
                'vendor a file under a name the daemon cannot import'
                % (value,))

    def test_a_path_shaped_file_par_is_reduced_to_its_basename(self):
        # Traversal is defused by taking the basename, not by rejecting
        # the entry -- every real `file` par IS a path.
        self.assertEqual(
            self.convoy._hostModuleName(_FakeDAT('x', '../evil.py')),
            'evil.py')

    def test_an_unusable_file_par_falls_back_to_the_dat_name(self):
        # An empty par, or one that basenames to nothing (a trailing
        # separator), is a DAT that is not externalized the way we
        # expected -- not a reason to drop the module out of the
        # payload. The fallback name is validated the same way, and the
        # vendor parity test is what catches a wrong one.
        for value in ('', '//server/share/x.py/'):
            self.assertEqual(
                self.convoy._hostModuleName(_FakeDAT('x', value)), 'x.py')

    def test_the_live_host_comp_yields_bare_python_filenames(self):
        # Runs against whatever is really vendored. Tolerant of the COMP
        # being absent (an upgraded .tox predating the vendoring step),
        # strict about what it returns when it is there.
        real = type(self.convoy)._hostModules
        self._patch(self.convoy, '_hostModules', lambda: real(self.convoy))
        modules = self.convoy._hostModules()
        self.assertIsInstance(modules, dict)
        if not modules:
            self.assertIsNone(self.comp.op('host'),
                              'a present `host` COMP that yields no modules '
                              'is a broken vendoring step, not an old .tox')
            return
        for name, text in modules.items():
            self.assertTrue(re.match(r'^[A-Za-z0-9._+-]+\.py$', name), name)
            self.assertTrue(text, '%s vendored empty' % (name,))


FAKE_VENV_PY = 'C:/fake/project/.venv/Scripts/python.exe'
FAKE_SYS_PY = 'C:/fake/usr/bin/python3'

DLOPEN_STDERR = (
    'Traceback (most recent call last):\n'
    '  File "<string>", line 8, in <module>\n'
    'ImportError: dlopen(/fake/.venv/site-packages/cryptography/hazmat/'
    'bindings/_rust.abi3.so, 0x0002): tried: mach-o file, but is an '
    "incompatible architecture (have 'arm64', need 'x86_64')")

NO_MODULE_STDERR = ("ModuleNotFoundError: No module named 'cryptography'")

SIGNATURE_STDERR = (
    'Traceback (most recent call last):\n'
    '  File "<string>", line 8, in <module>\n'
    'ImportError: dlopen(/fake/.venv/site-packages/cryptography/hazmat/'
    'bindings/_rust.abi3.so, 0x0002): code signature not valid for use in '
    'process: mapping process and mapped file (non-platform) have '
    'different Team IDs')


class TestVenvRuntimeInstall(ConvoyHostBase):
    """The venv-runtime probe loop, its one-shot repair, and its words.

    Calls the module-level _host_install directly (the worker body), the
    way _beginHostCall's synchronous stand-in would run it -- the
    orchestration above it is covered by TestInstallOrchestration. The
    probe seam is scripted per interpreter so no test ever spawns one.
    """

    def _ctx(self, **overrides):
        ctx = {'client': self.client, 'installer': self.installer,
               'platform': 'win32', 'data_dir': FAKE_DATA_DIR,
               'version': VERSION, 'home': FAKE_HOME, 'uid': None,
               'installed_by': 'C:/fake/project (/embody/Embody)',
               'health_wait_s': 0.0, 'health_poll_s': 0.0,
               'venv_python': FAKE_VENV_PY,
               'runtime_candidates': [FAKE_VENV_PY, FAKE_SYS_PY],
               'uv': 'C:/fake/uv.exe',
               'venv_python_repair': 'C:/fake/project/.venv/Scripts/'
                                     'python.exe',
               'venv_crypto_deps': ['cryptography>=3.4']}
        ctx.update(overrides)
        return ctx

    def _script_probes(self, outcomes):
        """installer.probe_runtime -> scripted per-interpreter results.

        `outcomes[path]` is a list consumed one result per probe of that
        path (the last repeats), so fail-then-pass sequences can model a
        repair that worked.
        """
        remaining = {k: list(v) for k, v in outcomes.items()}
        probed = []

        def probe(interp, platform=None, architecture=None, runner=None):
            probed.append(interp)
            queue = remaining.get(interp)
            if not queue:
                return {'ok': False, 'reason': 'runtime_probe_failed',
                        'detail': 'unscripted interpreter %s' % (interp,)}
            result = queue.pop(0) if len(queue) > 1 else queue[0]
            return dict(result)

        self.installer.probe_runtime = probe
        return probed

    def _repair_argvs(self):
        return [argv for name, argv in self.installer.calls
                if name == 'run_command' and '--reinstall' in argv]

    def _run_install(self, ctx):
        return self.convoy_mod._host_install(
            ctx, {'convoy_hostapp.py': 'x'}, ctx['venv_python'], None,
            venv_runtime=True)

    def test_repair_then_reprobe_recovers_the_venv(self):
        probed = self._script_probes({
            FAKE_VENV_PY: [
                {'ok': False, 'reason': 'runtime_crypto_broken',
                 'detail': DLOPEN_STDERR},
                {'ok': True, 'probe': {}}],
            FAKE_SYS_PY: [
                {'ok': False, 'reason': 'runtime_missing_cryptography',
                 'detail': NO_MODULE_STDERR}],
        })
        result = self._run_install(self._ctx())
        self.assertTrue(result['ok'], result)
        self.assertEqual(probed,
                         [FAKE_VENV_PY, FAKE_SYS_PY, FAKE_VENV_PY])
        repairs = self._repair_argvs()
        self.assertEqual(len(repairs), 1, 'exactly ONE repair, never a loop')
        argv = repairs[0]
        self.assertEqual(argv[0], 'C:/fake/uv.exe')
        self.assertIn('--no-cache', argv)
        self.assertIn('cryptography>=3.4', argv)
        self.assertEqual(argv[-2:],
                         ['--python', self._ctx()['venv_python_repair']])
        sent = self.installer.last('install')
        self.assertEqual(sent['interpreter'], FAKE_VENV_PY,
                         'the REPAIRED venv must be the daemon interpreter')

    def test_failed_reprobe_reports_the_repair_and_every_probe(self):
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False, 'reason': 'runtime_crypto_broken',
                            'detail': DLOPEN_STDERR}],
            FAKE_SYS_PY: [{'ok': False,
                           'reason': 'runtime_missing_cryptography',
                           'detail': NO_MODULE_STDERR}],
        })
        result = self._run_install(self._ctx())
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'no_usable_runtime')
        self.assertEqual(len(self._repair_argvs()), 1)
        detail = result['detail']
        self.assertIn('runtime_crypto_broken', detail)
        self.assertIn('incompatible architecture', detail,
                      'the dlopen diagnosis must reach the WARNING')
        self.assertIn('runtime_missing_cryptography', detail)
        self.assertIn('Repair was attempted', detail)
        self.assertIn('toggle Envoy off and on', detail,
                      'the failure must name its one next action')
        self.assertTrue(isinstance(result.get('rejected'), list)
                        and result['rejected'],
                        'the structured probe records must ride along')

    def test_no_repair_context_skips_the_reinstall(self):
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False, 'reason': 'runtime_crypto_broken',
                            'detail': DLOPEN_STDERR}],
            FAKE_SYS_PY: [{'ok': False,
                           'reason': 'runtime_missing_cryptography',
                           'detail': NO_MODULE_STDERR}],
        })
        result = self._run_install(self._ctx(uv=None))
        self.assertFalse(result['ok'])
        self.assertEqual(self._repair_argvs(), [],
                         'no uv means no repair subprocess at all')
        self.assertIn('Venv repair not possible', result['detail'])

    def test_first_passing_candidate_skips_repair_entirely(self):
        self._script_probes({FAKE_VENV_PY: [{'ok': True, 'probe': {}}]})
        result = self._run_install(self._ctx())
        self.assertTrue(result['ok'], result)
        self.assertEqual(self._repair_argvs(), [])

    def test_signature_failure_skips_the_repair(self):
        """Code-signing policy is not a wheel problem: no reinstall may
        run, the note must say why, and the venv-rebuild guidance must
        NOT appear -- it reproduces the failure exactly."""
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False,
                            'reason': 'runtime_crypto_signature_blocked',
                            'detail': SIGNATURE_STDERR}],
            FAKE_SYS_PY: [{'ok': False,
                           'reason': 'runtime_missing_cryptography',
                           'detail': NO_MODULE_STDERR}],
        })
        result = self._run_install(self._ctx())
        self.assertFalse(result['ok'])
        self.assertEqual(self._repair_argvs(), [],
                         'reinstalling cannot change code-signing policy')
        detail = result['detail']
        self.assertIn('Venv repair skipped', detail)
        self.assertIn('library validation', detail)
        self.assertIn('brew install python', detail,
                      'the failure must name a concrete way out')
        self.assertNotIn('toggle Envoy off and on', detail,
                         'the rebuild advice reproduces this failure')

    def test_finish_host_dumps_rejected_probes_at_debug(self):
        details = []
        self._patch(self.convoy, '_log',
                    lambda msg, level='INFO', details_=None, **kw:
                        details.append((msg, level,
                                        kw.get('details', details_))))
        self.convoy._finishHost('install', {
            'ok': False, 'reason': 'no_usable_runtime',
            'detail': 'no interpreter on this machine could load ...',
            'rejected': [{'candidate': FAKE_VENV_PY,
                          'reason': 'runtime_crypto_broken',
                          'detail': DLOPEN_STDERR}]})
        debug = [(m, lvl, det) for m, lvl, det in details if lvl == 'DEBUG']
        self.assertTrue(debug, 'each rejected probe must be DEBUG-logged')
        self.assertIn(FAKE_VENV_PY, debug[0][0])
        self.assertIn('incompatible architecture', str(debug[0][2]),
                      'the FULL stderr must reach the ring buffer')


FAKE_DAEMON_VENV = {'dir': 'C:/fake/EmbodyConvoy/runtime-venv',
                    'python': 'C:/fake/EmbodyConvoy/runtime-venv/bin/'
                              'python3',
                    'bases': ['/opt/homebrew/bin/python3']}


class TestDaemonVenvFallback(ConvoyHostBase):
    """The macOS ladder's last rung: build a venv OUTSIDE TouchDesigner.

    Direct calls to the module-level _host_install with a darwin-shaped
    ctx; run_command is scripted so the base-version gate, uv venv, and
    uv pip steps are observable without spawning anything.
    """

    def _ctx(self, **overrides):
        ctx = {'client': self.client, 'installer': self.installer,
               'platform': 'darwin', 'data_dir': FAKE_DATA_DIR,
               'version': VERSION, 'home': FAKE_HOME, 'uid': 501,
               'installed_by': '/fake/project (/embody/Embody)',
               'health_wait_s': 0.0, 'health_poll_s': 0.0,
               'venv_python': FAKE_VENV_PY,
               'runtime_candidates': [FAKE_VENV_PY],
               'uv': '/fake/uv',
               'venv_python_repair': FAKE_VENV_PY,
               'venv_crypto_deps': ['cryptography>=3.4'],
               'daemon_venv': dict(FAKE_DAEMON_VENV)}
        ctx.update(overrides)
        return ctx

    def _script_probes(self, outcomes):
        remaining = {k: list(v) for k, v in outcomes.items()}
        probed = []

        def probe(interp, platform=None, architecture=None, runner=None):
            probed.append(interp)
            queue = remaining.get(interp)
            if not queue:
                return {'ok': False, 'reason': 'runtime_probe_failed',
                        'detail': 'unscripted interpreter %s' % (interp,)}
            result = queue.pop(0) if len(queue) > 1 else queue[0]
            return dict(result)

        self.installer.probe_runtime = probe
        return probed

    def _script_run_command(self, base_version='3.12'):
        """run_command double: version probes answer, uv calls succeed."""
        def run_command(argv, timeout_s=None, **kw):
            self.installer.calls.append(('run_command', list(argv)))
            if argv and argv[0] in ('/opt/homebrew/bin/python3',
                                    '/usr/local/bin/python3',
                                    '/usr/bin/python3'):
                return 0, base_version + '\n', ''
            return 0, '', ''

        self.installer.run_command = run_command

    def _uv_argvs(self, marker):
        return [argv for name, argv in self.installer.calls
                if name == 'run_command' and argv[:2] == ['/fake/uv',
                                                          marker]]

    def _repair_argvs(self):
        return [argv for name, argv in self.installer.calls
                if name == 'run_command' and '--reinstall' in argv]

    def _run_install(self, ctx):
        return self.convoy_mod._host_install(
            ctx, {'convoy_hostapp.py': 'x'}, ctx['venv_python'], None,
            venv_runtime=True)

    def test_signature_blocked_venv_gets_a_daemon_venv(self):
        self._script_run_command()
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False,
                            'reason': 'runtime_crypto_signature_blocked',
                            'detail': SIGNATURE_STDERR}],
            FAKE_DAEMON_VENV['python']: [{'ok': True, 'probe': {}}],
        })
        result = self._run_install(self._ctx())
        self.assertTrue(result['ok'], result)
        self.assertEqual(self._repair_argvs(), [],
                         'signature failures never trigger the reinstall')
        venv_calls = self._uv_argvs('venv')
        self.assertEqual(len(venv_calls), 1, 'exactly ONE build, no loop')
        self.assertIn('--clear', venv_calls[0])
        self.assertIn('/opt/homebrew/bin/python3', venv_calls[0])
        pip_calls = self._uv_argvs('pip')
        self.assertEqual(len(pip_calls), 1)
        self.assertIn('cryptography>=3.4', pip_calls[0])
        self.assertEqual(pip_calls[0][-2:],
                         ['--python', FAKE_DAEMON_VENV['python']])
        sent = self.installer.last('install')
        self.assertEqual(sent['interpreter'], FAKE_DAEMON_VENV['python'],
                         'the daemon must run under the dedicated venv')
        self.assertIn('dedicated Convoy venv', result['detail'])

    def test_no_base_interpreter_names_the_way_out(self):
        self._script_run_command()
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False,
                            'reason': 'runtime_crypto_signature_blocked',
                            'detail': SIGNATURE_STDERR}],
        })
        daemon = dict(FAKE_DAEMON_VENV, bases=[])
        result = self._run_install(self._ctx(daemon_venv=daemon))
        self.assertFalse(result['ok'])
        self.assertEqual(self._uv_argvs('venv'), [],
                         'nothing to build from -- no uv subprocess')
        self.assertIn('brew install python', result['detail'])

    def test_old_base_python_is_version_gated_before_building(self):
        self._script_run_command(base_version='3.9')
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False,
                            'reason': 'runtime_crypto_signature_blocked',
                            'detail': SIGNATURE_STDERR}],
        })
        result = self._run_install(self._ctx())
        self.assertFalse(result['ok'])
        self.assertEqual(self._uv_argvs('venv'), [],
                         'a 3.9 base must be rejected BEFORE the build')
        self.assertIn('brew install python', result['detail'])

    def test_build_success_but_probe_failure_is_honest(self):
        self._script_run_command()
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False,
                            'reason': 'runtime_crypto_signature_blocked',
                            'detail': SIGNATURE_STDERR}],
            FAKE_DAEMON_VENV['python']: [{'ok': False,
                                          'reason': 'runtime_probe_failed',
                                          'detail': 'daemon venv died'}],
        })
        result = self._run_install(self._ctx())
        self.assertFalse(result['ok'])
        self.assertEqual(len(self._uv_argvs('venv')), 1,
                         'one build, never a retry loop')
        self.assertTrue(any(r.get('candidate') == FAKE_DAEMON_VENV['python']
                            for r in result.get('rejected', [])),
                        'the fallback probe record must ride along')

    def test_without_a_daemon_venv_spec_nothing_is_built(self):
        # The Windows shape: ctx carries no daemon_venv (spec is None off
        # darwin), so all-candidates-failed ends the story with no build.
        self._script_run_command()
        self._script_probes({
            FAKE_VENV_PY: [{'ok': False, 'reason': 'runtime_crypto_broken',
                            'detail': DLOPEN_STDERR}],
        })
        ctx = self._ctx(platform='win32', daemon_venv=None,
                        venv_crypto_deps=[])
        result = self._run_install(ctx)
        self.assertFalse(result['ok'])
        self.assertEqual(self._uv_argvs('venv'), [])


class TestHostThreadingDiscipline(EmbodyTestCase):
    """The rule that froze TD in the field, pinned at the source level."""

    def setUp(self):
        super().setUp()
        self.comp = _convoy_comp(self)
        self.src = self.comp.op('ConvoyExt').text

    def test_the_installer_module_is_resolved_exactly_once(self):
        self.assertEqual(
            self.src.count('mod.convoy_install'), 1,
            'exactly one mod.convoy_install reference (inside _installer), '
            'or a worker is re-resolving a DAT off the main thread -- '
            '`mod.name` is a LIVE DAT LOOKUP')

    def test_every_worker_body_is_module_level(self):
        """A bound method would carry `self`, and `self` is one attribute
        away from an operator, a parameter and a DAT."""
        for name in ('_host_snapshot', '_host_install', '_host_start',
                     '_host_stop', '_host_preview', '_host_uninstall',
                     '_host_shutdown', '_host_await_health',
                     '_host_is_running', '_host_repair_venv_runtime',
                     '_host_build_daemon_venv'):
            self.assertIn('\ndef %s(' % (name,), self.src,
                          '%s must be a module-level function, not a method'
                          % (name,))

    def test_no_worker_body_touches_a_td_object(self):
        tail = self.src.split('HOST-APP WORKER BODIES')[-1]
        # WORD-BOUNDED, or 'stop(' would trip the 'op(' rule and
        # 'time.' would trip 'me.'. The point is a TD access, not a
        # substring.
        for banned in (r'\bself\.', r'\bop\(', r'\bopex\(', r'\bparent\.',
                       r'\brun\(', r'\bme\.', r'\.par\.', r'\bdebug\(',
                       r'\bmod\.', r'\bui\.', r'\bproject\.'):
            found = re.search(banned, tail)
            self.assertIsNone(
                found,
                'the worker bodies must contain no %r (found %r) -- from a '
                'worker thread a READ is treated exactly like a write'
                % (banned, found.group(0) if found else None))

    def test_the_promoted_host_api_exists(self):
        for name in ('InstallHost', 'StartHost', 'StopHost', 'UninstallHost',
                     'PreviewHostUninstall', 'HostStatus'):
            self.assertTrue(callable(getattr(self.comp.ext.ConvoyExt, name,
                                             None)),
                            'ConvoyExt must expose a callable %s' % (name,))

    def test_this_suite_is_not_destructive(self):
        # It orchestrates against a stub; it must never be tagged into the
        # save-gated batch, or a normal run stops covering it.
        self.assertFalse(getattr(TestInstallOrchestration, 'DESTRUCTIVE',
                                 False))
        self.assertFalse(getattr(TestUninstallSafety, 'DESTRUCTIVE', False))


class TestForgetOfflineNodes(ConvoyHostBase):
    """The human-judgment cleanup path: enumerating consent, then the
    daemon's own refusal rules per row. Offline retention is the
    remote-start feature, so nothing here may forget silently."""

    ROWS = (200, {'ok': True, 'host_id': 'h' * 32, 'nodes': [
        {'host_id': 'h' * 32, 'node_id': 'a' * 32, 'online': False,
         'node_name': 'TEC-X / e2', 'toe_name': 'e2.1.toe',
         'last_seen_age_s': 7200.0},
        {'host_id': 'h' * 32, 'node_id': 'b' * 32, 'online': False,
         'node_name': 'TEC-X / e3', 'toe_name': 'e3.1.toe',
         'last_seen_age_s': 3600.0},
        {'host_id': 'h' * 32, 'node_id': 'c' * 32, 'online': True,
         'node_name': 'TEC-X / live', 'toe_name': 'live.1.toe',
         'last_seen_age_s': 3.0},
        {'host_id': 'p' * 32, 'node_id': 'd' * 32, 'online': False,
         'node_name': 'PEER / other', 'toe_name': 'other.1.toe',
         'last_seen_age_s': 9999.0},
    ]})

    def setUp(self):
        super().setUp()
        self.client.probe_status = self.client.STATUS_RUNNING
        self.client.get_results = [self.ROWS]

    def test_the_dialog_names_offline_rows_and_cancel_touches_nothing(self):
        self.choice = 0                       # Cancel
        self.convoy.ForgetOfflineNodes()
        self.assertLen(self.dialogs, 1)
        _title, message, buttons = self.dialogs[0]
        self.assertEqual(buttons[0], 'Cancel', 'the safe answer is button 0')
        self.assertIn('TEC-X / e2', message)
        self.assertIn('TEC-X / e3', message)
        self.assertNotIn('TEC-X / live', message,
                         'an online row must never be offered')
        self.assertNotIn('PEER / other', message,
                         'peer rows are not ours to forget')
        self.assertIn('NEW identity', message)
        self.assertEqual(self.client.count('host_post'), 0,
                         'Cancel must not touch the daemon')

    def test_confirm_forgets_each_offline_row(self):
        self.choice = 1
        self.convoy.ForgetOfflineNodes()
        forgets = [body for path, body in self.client.posted
                   if path == '/nodes/forget']
        self.assertEqual([b['node_id'] for b in forgets],
                         ['a' * 32, 'b' * 32])
        self.assertTrue(any('forgot 2' in m for m, _l in self._logs),
                        self._logs)

    def test_a_row_back_online_by_apply_time_is_skipped(self):
        rows_later = (200, {'ok': True, 'host_id': 'h' * 32, 'nodes': [
            dict(self.ROWS[1]['nodes'][0]),                  # e2 offline
            dict(self.ROWS[1]['nodes'][1], online=True),     # e3 came back
        ]})
        self.client.get_results = [self.ROWS, rows_later]
        self.choice = 1
        self.convoy.ForgetOfflineNodes()
        forgets = [body for path, body in self.client.posted
                   if path == '/nodes/forget']
        self.assertEqual([b['node_id'] for b in forgets], ['a' * 32],
                         'a row that came back online is never forgotten')
        self.assertTrue(any('skipped 1' in m for m, _l in self._logs))

    def test_unresolved_work_is_kept_and_reported(self):
        self.choice = 1

        def refuse_busy(path, body):
            if body.get('node_id') == 'b' * 32:
                return 409, {'ok': False, 'reason': 'node_has_work'}
            return 200, {'ok': True, 'forgotten': True}

        self.client.post_result = refuse_busy
        self.convoy.ForgetOfflineNodes()
        self.assertTrue(
            any('kept 1 with unresolved jobs' in m for m, _l in self._logs),
            self._logs)

    def test_no_offline_rows_means_no_dialog(self):
        self.client.get_results = [
            (200, {'ok': True, 'host_id': 'h' * 32, 'nodes': []})]
        self.convoy.ForgetOfflineNodes()
        self.assertLen(self.dialogs, 0)
        self.assertTrue(any('no offline nodes' in m for m, _l in self._logs))

    def test_a_busy_host_slot_ignores_the_pulse(self):
        import time as _time
        self.convoy._host_busy = True
        self.convoy._host_busy_since = _time.time()
        got = self.convoy.ForgetOfflineNodes()
        self.assertEqual(got['state'], 'busy')
        self.assertLen(self.dialogs, 0)
        self.convoy._host_busy = False
