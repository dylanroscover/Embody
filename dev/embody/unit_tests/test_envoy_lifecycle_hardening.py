"""
Test suite: Envoy lifecycle hardening contracts (dev/embody/plan-envoy-resilience.md v2).

H1 -- "Running" means a CONFIRMED bind, never merely "task enqueued":
  - A 'Starting' state is visible before bind confirmation.
  - A never-bound start (EnqueueTask returns None) must NOT report Running.
  - A bind failure (worker errors) must NOT leave a Running status.

H1 tests are state/mocked -- they do NOT start a real server. They drive
_continueStart() with the ThreadManager + config writes + the module-level run()
scheduler patched out, so the frame-scheduled _pollStartup never fires and the
synchronous failure paths are exercised directly.

(H3 restart-accounting contracts and H4 live save/export contracts are added in
later stages of the build, per the staged integration plan.)
"""

import sys
import time

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class _FakeTask:
    def __init__(self, target=None, args=(), SuccessHook=None,
                 ExceptHook=None, RefreshHook=None):
        self.target = target
        self.args = args
        self.SuccessHook = SuccessHook
        self.ExceptHook = ExceptHook
        self.RefreshHook = RefreshHook


class _FakeThread:
    pass


class _FakeThreadManager:
    def __init__(self, enqueue):
        self._enqueue = enqueue
        self.enqueued = []
        self.TDTask = _FakeTask

    def EnqueueTask(self, task, standalone=True):
        self.enqueued.append((task, standalone))
        return self._enqueue(task, standalone)


class EnvoyLifecycleContractBase(EmbodyTestCase):
    """Shared fixture for state/mocked lifecycle contracts (no real server)."""

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.envoy_mod = self.embody.op('EnvoyExt').module
        self._patches = []
        self._runs = []

        self._saved_enable = self.embody.par.Envoyenable.eval()
        self._saved_status = self.embody.par.Envoystatus.eval()
        self._saved_running = self.embody.fetch('envoy_running', None, search=False)
        self._saved_starting = getattr(self.envoy, '_starting', False)

        # CRITICAL: _continueStart replaces the live server wiring (queues,
        # generation, shutdown_event, current_task).  If we don't restore it,
        # the running MCP server's worker keeps the OLD queues while _onRefresh
        # drains the NEW empty ones -> every MCP call hangs.  Snapshot it all.
        self._saved_state = {
            'request_queue': self.envoy.request_queue,
            'response_queue': self.envoy.response_queue,
            'server_gen': self.envoy._server_gen,
            'current_task': self.envoy.current_task,
            'shutdown_event': self.envoy.shutdown_event,
            'sys_queues': dict(getattr(sys, '_envoy_queues', {})),
            'sys_shutdown': dict(getattr(sys, '_envoy_shutdown_events', {})),
        }

        # Baseline for the mocked contracts: simulate a clean, not-yet-running
        # start. The LIVE server is actually running (envoy_running=True), but
        # these tests drive _continueStart in isolation -- restored in tearDown.
        self.embody.store('envoy_running', False)
        self.envoy._starting = False

        self._parexec = self.embody.op('parexec')
        self._saved_parexec_active = None
        if self._parexec is not None:
            self._saved_parexec_active = self._parexec.par.active.eval()
            self._parexec.par.active = 0

        # Suppress the frame-scheduled _pollStartup / restart run() chains so
        # mocked tests stay synchronous and never touch a real server.
        self._patch(self.envoy_mod, 'run',
                    lambda *a, **k: self._runs.append((a, k)))

    def tearDown(self):
        while self._patches:
            obj, name, old, sentinel = self._patches.pop()
            if old is sentinel:
                try:
                    delattr(obj, name)
                except Exception:
                    pass
            else:
                setattr(obj, name, old)

        # Restore live server wiring so the running MCP server is never left
        # reading swapped-out queues (which would hang all MCP calls).
        st = self._saved_state
        self.envoy.request_queue = st['request_queue']
        self.envoy.response_queue = st['response_queue']
        self.envoy._server_gen = st['server_gen']
        self.envoy.current_task = st['current_task']
        self.envoy.shutdown_event = st['shutdown_event']
        sys._envoy_queues = st['sys_queues']
        sys._envoy_shutdown_events = st['sys_shutdown']

        self.envoy._starting = self._saved_starting
        if self._saved_running is None:
            try:
                self.embody.unstore('envoy_running')
            except Exception:
                pass
        else:
            self.embody.store('envoy_running', self._saved_running)
        self.embody.par.Envoystatus = self._saved_status
        self.embody.par.Envoyenable = self._saved_enable
        if self._parexec is not None and self._saved_parexec_active is not None:
            self._parexec.par.active = self._saved_parexec_active

        super().tearDown()

    def _patch(self, obj, name, value):
        sentinel = object()
        old = getattr(obj, name, sentinel)
        setattr(obj, name, value)
        self._patches.append((obj, name, old, sentinel))

    def _status(self):
        return str(self.embody.par.Envoystatus.eval())

    def _prepareMockedStart(self, enqueue, port=None):
        if port is None:
            port = int(self.embody.par.Envoyport.eval())
        self._patch(self.envoy, '_findAvailablePort',
                    lambda base_port, range_size=10: port)
        self._patch(self.envoy, '_cleanupTempFiles', lambda: None)
        self._patch(self.envoy, '_cleanupStaleThreads', lambda: None)
        self._patch(self.envoy, '_configureMCPClient', lambda *a, **k: None)
        self._patch(self.envoy, '_configureGitignore', lambda *a, **k: None)
        self._patch(self.envoy, '_configureGitattributes', lambda *a, **k: None)
        self._patch(self.embody_ext, '_upgradeEnvoy', lambda *a, **k: None)
        self._patch(self.embody_ext, '_findProjectRoot', lambda *a, **k: 'no-git')
        self._patch(self.envoy, 'ThreadManager', _FakeThreadManager(enqueue))


class TestH1StartupStatusTruth(EnvoyLifecycleContractBase):
    """MOCKED: no real server start. Verifies status reflects bind, not enqueue."""

    def test_H1_starting_state_before_bind_confirmation(self):
        seen = []

        def enqueue(task, standalone):
            seen.append((self._status(),
                         bool(self.embody.fetch('envoy_running', False, search=False))))
            return _FakeThread()

        self.embody.par.Envoyenable = 0
        self._prepareMockedStart(enqueue)
        self.envoy._continueStart('no-git')

        self.assertTrue(
            any(s.startswith('Starting') for s, _ in seen),
            f'A Starting state must be visible before bind confirmation; saw {seen}')
        self.assertFalse(
            any(s.startswith('Starting') and running for s, running in seen),
            'envoy_running must stay False while only Starting')

    def test_H1_never_bound_start_does_not_report_running(self):
        def enqueue(task, standalone):
            # No standalone worker -> the socket can never bind. The old code
            # set Running anyway; H1 must route to a failure, not a zombie.
            return None

        self.embody.par.Envoyenable = 0
        self._prepareMockedStart(enqueue)
        self.envoy._continueStart('no-git')

        self.assertFalse(
            self._status().startswith('Running'),
            f'Never-bound start must not report Running, got: {self._status()}')
        self.assertFalse(
            self.embody.fetch('envoy_running', False, search=False),
            'envoy_running must stay False until bind is confirmed')
        self.assertFalse(
            self.envoy._starting,
            '_starting must be cleared after a never-bound failure')

    def test_H1_bind_failure_does_not_leave_running_status(self):
        def enqueue(task, standalone):
            task.ExceptHook(RuntimeError('Port 9870 is already in use'))
            return _FakeThread()

        self.embody.par.Envoyenable = 0
        self._prepareMockedStart(enqueue, port=9870)
        self.envoy._continueStart('no-git')

        self.assertFalse(
            self._status().startswith('Running'),
            f'Bind failure must not leave Running status, got: {self._status()}')
        self.assertFalse(
            self.embody.fetch('envoy_running', False, search=False),
            'envoy_running must be False after a bind failure')

    def test_H1_starting_window_suppresses_duplicate_start(self):
        # While _starting is open, a second Start() must be a no-op (it can no
        # longer rely on envoy_running, which is deferred until confirmed bind).
        def enqueue(task, standalone):
            return _FakeThread()

        self.embody.par.Envoyenable = 0
        self._prepareMockedStart(enqueue)
        self.envoy._continueStart('no-git')
        # _continueStart leaves _starting True (poll deferred via patched run()).
        self.assertTrue(self.envoy._starting,
                        'starting window should be open after _continueStart')
        # Start() must short-circuit while starting.
        before = len(self.envoy.ThreadManager.enqueued)
        self.envoy.Start()
        after = len(self.envoy.ThreadManager.enqueued)
        self.assertEqual(before, after,
                         'Start() must be a no-op while a start is in progress')
