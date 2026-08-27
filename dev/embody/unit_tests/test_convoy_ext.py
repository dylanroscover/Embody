"""
Test suite: ConvoyExt, the node-side Convoy reconciler (Phase 2 Stage B).

NORMAL TIER. Nothing here touches ext.root, the live Convoy parameters, the
real host app, a socket, or a thread. Three seams make that true:

  - ``_client`` is patched to a StubClient that WRAPS the real
    convoy_client module and replaces only its network functions
    (registration, directory and sibling relay calls). Every pure helper the extension
    depends on -- status_text, backoff_delay, ensure_runtime_id,
    registration_payload -- is therefore the SHIPPING implementation, not
    a mock that could agree with a bug.
  - ``_runInWorker`` is patched to run the worker body synchronously, so a
    whole register round trip completes inside one test method with no
    thread and no timing race.
  - the module-level ``run()`` scheduler is patched to RECORD instead of
    dispatch, so no tick and no poll is ever really scheduled.

The live parameters are never written: _status / _publishId / _setEnabled
are patched to recorders. The live per-process session dict is never
touched either -- _session is patched to a private dict, so a test can
never disturb (or inherit) the real runtime_id or node_id.

The par-toggle path is asserted at the SOURCE level (parexec / execute)
rather than by flipping Convoyenable. Parameter Execute callbacks apply at
the frame boundary, after a synchronous test method has returned -- the
same reason test_component_presentation pins Showbuiltinpars through the
source -- and here it matters more than style: a real flip would call
Register() for real, which on a machine whose project.json already carries
a convoy id would reach the real host app.

Coverage:
  - Registrations: Convoyenable in _PERSISTED_PARAMS, the Phase 3 danger
    gates NOT in it (A-49), Convoystatus/Convoyid in
    _TRANSIENT_STATUS_PARS['Embody'] (the A-50 leak stop), _convoy_gen in
    TDXNExt's SKIP_STORAGE_KEYS, EnvoyExt.RuntimePort() exists.
  - Reconcile: an unchanged tuple issues ZERO calls; enable -> disable
    drives exactly one register and one unregister; a changed Envoy port
    re-registers with the SAME runtime_id.
  - Refusals that are not errors: Perform Mode does zero work and never
    writes the status par; an unsaved project refuses; a missing convoy id
    never mints on a tick; absence never logs a WARNING.
  - Tick hygiene: a stale generation collapses (no reconcile, no
    reschedule); a superseded poll generation retries instead of applying;
    a stale INSTANCE drops its poll entirely.
  - A-13: confirm mints and records the scope; cancel (and a suppressed
    dialog's -1) flips the toggle back off, writes nothing, and is not an
    error; an already-recorded convoy never re-prompts.
"""

import os
import re
import time
import types
from collections import OrderedDict
from queue import Queue
from threading import Event, Lock, Thread

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

CONSENT_SCOPE = 'trusted LAN Convoy mesh'
LEGACY_CONSENT_SCOPE = 'local host app only'
NODE_ID = 'n' * 32
HOST_ID = 'h' * 32
OTHER_HOST_ID = 'x' * 32
CONVOY_ID = 'cv_' + '0' * 16


class StubClient:
    """The REAL convoy_client with its network seams replaced.

    Attribute lookups fall through to the live module, so every constant
    and every pure helper under test is the shipping one. Only probe,
    register, unregister, directory and sibling-job functions are stubs --
    exactly the set that would otherwise open a socket.
    """

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.probe_result = None      # set by each test
        self.register_result = {'state': 'registered', 'node_id': NODE_ID,
                                'host_id': HOST_ID, 'runtime_id': None,
                                'envoy_port': 9870}
        self.unregister_result = {'state': 'unregistered', 'node_id': NODE_ID,
                                  'cleared': True}
        self.network_result = {
            'state': 'nodes',
            'convoy_id': CONVOY_ID,
            'nodes': [{
                'node_id': NODE_ID,
                'host_id': HOST_ID,
                'convoy_id': CONVOY_ID,
                'runtime_id': 'rt_test',
                'node_name': 'render-01 / project',
                'hostname': 'render-01',
                'toe_name': 'project.toe',
                'embody_version': '6.0.178',
                'touchdesigner_version': '2025.32180',
                'ip': '127.0.0.1',
                'status': 'online',
                'online': True,
                'enabled': True,
            }],
            'truncated': False,
            'remote_nodes_available': True,
        }
        self.sibling_submit_result = {
            'state': 'accepted', 'ok': True,
            'target_host_id': HOST_ID, 'convoy_id': CONVOY_ID,
            'target_node_id': NODE_ID, 'operation': 'query_network',
            'idempotency_key': 'stub',
            'job': {'delivery_id': 'cj_stub', 'state': 'queued',
                    'node_id': NODE_ID, 'operation': 'query_network'},
        }
        self.sibling_wait_result = {
            'state': 'job', 'ok': True, 'changed': True,
            'delivery_id': 'cj_stub',
            'job': {'delivery_id': 'cj_stub', 'state': 'succeeded',
                    'node_id': NODE_ID, 'operation': 'query_network',
                    'result': {'ok': True}},
        }
        self.sibling_get_result = dict(self.sibling_wait_result)
        self.sibling_cancel_result = {
            'state': 'cancel', 'ok': True, 'cancelled': True,
            'definitive': True, 'wakes_touchdesigner': False,
        }

    def __getattr__(self, name):
        return getattr(self._real, name)

    # -- replaced network seams ---------------------------------------
    def probe(self, *args, **kwargs):
        self.calls.append(('probe', None))
        if self.probe_result is None:
            return self.running()
        return self.probe_result

    def register(self, handle, payload, **kwargs):
        self.calls.append(('register', dict(payload)))
        return dict(self.register_result)

    def unregister(self, handle, node_id, runtime_id=None, **kwargs):
        self.calls.append(('unregister', {'node_id': node_id,
                                          'runtime_id': runtime_id,
                                          'reason': kwargs.get('reason')}))
        return dict(self.unregister_result)

    def network_nodes(self, handle, convoy_id, **kwargs):
        self.calls.append(('network_nodes', {'convoy_id': convoy_id}))
        return dict(self.network_result)

    def submit_sibling_call(self, handle, target_host_id, convoy_id,
                            target_node_id, controller_id, operation,
                            arguments=None, **kwargs):
        self.calls.append(('submit_sibling_call', {
            'target_host_id': target_host_id,
            'convoy_id': convoy_id,
            'target_node_id': target_node_id,
            'controller_id': controller_id,
            'operation': operation,
            'arguments': dict(arguments or {}),
            'expected_runtime_id': kwargs.get('expected_runtime_id'),
            'idempotency_key': kwargs.get('idempotency_key'),
            'timeout_s': kwargs.get('timeout_s'),
        }))
        out = dict(self.sibling_submit_result)
        out.update({'target_host_id': target_host_id,
                    'convoy_id': convoy_id,
                    'target_node_id': target_node_id,
                    'operation': operation,
                    'idempotency_key': kwargs.get('idempotency_key')})
        out['job'] = dict(self.sibling_submit_result.get('job') or {})
        return out

    def wait_sibling_job(self, handle, target_host_id, convoy_id,
                         delivery_id, initial=None, progress=None, **kwargs):
        self.calls.append(('wait_sibling_job', {
            'target_host_id': target_host_id, 'convoy_id': convoy_id,
            'delivery_id': delivery_id,
            'timeout_s': kwargs.get('timeout_s'),
        }))
        if callable(progress):
            progress(dict(self.sibling_wait_result))
        out = dict(self.sibling_wait_result)
        out['job'] = dict(self.sibling_wait_result.get('job') or {})
        return out

    def get_sibling_job(self, handle, target_host_id, convoy_id,
                        delivery_id, **kwargs):
        self.calls.append(('get_sibling_job', {
            'target_host_id': target_host_id, 'convoy_id': convoy_id,
            'delivery_id': delivery_id, 'since': kwargs.get('since'),
            'timeout': kwargs.get('timeout'),
        }))
        out = dict(self.sibling_get_result)
        out['job'] = dict(self.sibling_get_result.get('job') or {})
        return out

    def cancel_sibling_job(self, handle, target_host_id, convoy_id,
                           delivery_id, **kwargs):
        self.calls.append(('cancel_sibling_job', {
            'target_host_id': target_host_id, 'convoy_id': convoy_id,
            'delivery_id': delivery_id,
            'timeout': kwargs.get('timeout'),
        }))
        return dict(self.sibling_cancel_result)

    # -- probe-result builders ----------------------------------------
    def running(self, host_id=HOST_ID):
        handle = self._real.HostHandle(port=41999, host_id=host_id,
                                       token='t' * 16, data_dir='')
        return self._real.ProbeResult(self._real.STATUS_RUNNING,
                                      handle=handle)

    def absent(self):
        return self._real.ProbeResult(
            self._real.STATUS_ABSENT,
            detail='no Convoy host app on this machine')

    def stale(self):
        return self._real.ProbeResult(
            self._real.STATUS_STALE,
            detail='host app portfile is stale (writer not alive)')

    def count(self, kind):
        return sum(1 for name, _payload in self.calls if name == kind)

    def last(self, kind):
        for name, payload in reversed(self.calls):
            if name == kind:
                return payload
        return None


class _ThreadManagerHarness:
    """Tiny real-thread stand-in for TDResources.ThreadManager.

    The shipping extension is still responsible for constructing TDTask and
    asking ThreadManager to enqueue it. Tests use Python threads only to make
    network-stub overlap observable without touching any TD object.
    """

    class _Task:
        def __init__(self, target, args):
            self.target = target
            self.args = args

    def __init__(self):
        self.threads = []
        self.errors = []
        self._lock = Lock()

    def TDTask(self, target, args=()):
        return self._Task(target, args)

    def EnqueueTask(self, task, standalone=True):
        def _run():
            try:
                task.target(*task.args)
            except Exception as e:
                with self._lock:
                    self.errors.append(e)

        thread = Thread(target=_run, daemon=True)
        with self._lock:
            self.threads.append(thread)
        thread.start()
        return thread

    def enqueue_callable(self, fn):
        return self.EnqueueTask(self.TDTask(fn), standalone=True) is not None

    def join_all(self, timeout=2.0):
        with self._lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(timeout)
        return all(not thread.is_alive() for thread in threads)


class TestConvoyRegistrations(EmbodyTestCase):
    """Registry entries and wiring that must exist BEFORE any behavior.

    These are the ones that ship a bug to every user when forgotten: an
    unregistered Convoyid bakes this machine's convoy into Embody.tdn and
    every released .tox (the A-50 leak class), and an unscrubbed
    Convoyenable would let a released .tox auto-enable Convoy.
    """

    def test_convoy_component_and_extension_exist(self):
        comp = self.embody.op('convoy')
        self.assertIsNotNone(
            comp, "the 'convoy' child COMP must exist inside the Embody COMP")
        self.assertTrue(
            callable(getattr(comp.ext.ConvoyExt, 'Register', None)),
            'ConvoyExt must be promoted on the convoy COMP with Register()')
        for name in ('Unregister', 'ConvoyStatus', '_convoyTick',
                     '_reconcile', '_desiredState', '_beginCall',
                     '_pollCall', 'listNodes', 'ping', 'call', 'batch',
                     'getJob', 'cancelJob', 'requestResult'):
            self.assertTrue(callable(getattr(comp.ext.ConvoyExt, name, None)),
                            'ConvoyExt must expose a callable %s' % (name,))

    def test_convoyenable_is_persisted(self):
        self.assertIn('Convoyenable', self.embody_ext._PERSISTED_PARAMS,
                      'the canonical gate must survive a restart')

    def test_phase3_danger_gates_are_not_persisted(self):
        # A-49: the dangerous gates must never be restorable from a config
        # file. They do not exist yet -- this pins the rule so adding them
        # to the whitelist later trips a test instead of shipping.
        for name in ('Convoyallowtdpython', 'Convoyallowfullshell',
                     'Convoyallowrepowrites'):
            self.assertNotIn(name, self.embody_ext._PERSISTED_PARAMS,
                             '%s must never be persisted (A-49)' % (name,))

    def test_convoy_readouts_are_registered_transients(self):
        """The Convoy readout must be scrubbed on export, and the removed
        ones must not come back unregistered.

        Convoystatus is the SINGLE readout (it merged the old Convoystatus
        + Convoyhoststatus pair). It is a state, so it names a truthy
        resting string. What matters for A-50 is that anything carrying
        machine identity is either absent from the page or registered:
        unregistered, a live readout bakes into the tracked Embody.tdn and
        into every released .tox.

        Convoyid and Convoyhoststatus were REMOVED from the page -- a
        truncated convoy hash, a truncated host hash and a process id are
        not things a user can act on. If either is ever restored it must
        arrive registered, so this asserts the pair-wise invariant rather
        than just their absence.
        """
        registry = self.embody_ext._TRANSIENT_STATUS_PARS['Embody']
        self.assertEqual(registry.get('Convoystatus'), 'Disabled')
        self.assertEqual(
            self.embody.par.Convoystatus.default, 'Disabled',
            'the resting and the default must agree, or a scrub and a '
            'revert-to-default disagree about the same par')
        self.assertTrue(
            self.embody.par.Convoystatus.readOnly,
            'Convoystatus is a readout, not an input')
        for name in ('Convoyid', 'Convoyhoststatus'):
            if getattr(self.embody.par, name, None) is not None:
                self.assertIn(
                    name, registry,
                    '%s is back on the Convoy page; it carries machine '
                    'identity, so it MUST be a registered transient or it '
                    'bakes into Embody.tdn and every released .tox (A-50)'
                    % name)

    def test_convoy_host_lifecycle_pulses_exist_and_are_documented(self):
        """The four host-app buttons must exist with help text.

        Installing registers a program that runs at LOGIN with TD closed
        -- a different grant from A-13's convoy consent -- so the help is
        the only place a user reads what they are agreeing to before the
        confirmation dialog. A pulse with no help ships a button whose
        meaning lives only in the changelog.
        """
        for name in ('Convoyinstallhost', 'Convoystarthost',
                     'Convoystophost', 'Convoyuninstallhost'):
            par = getattr(self.embody.par, name, None)
            self.assertIsNotNone(par, '%s must exist on the Embody COMP'
                                 % name)
            self.assertEqual(par.style, 'Pulse',
                             '%s must be a Pulse (fire-once action)' % name)
            self.assertTrue(
                par.help and len(par.help) > 40,
                '%s needs help text that explains what it does, not a '
                'restatement of its label' % name)

    def test_convoy_generation_never_serializes(self):
        skip = self.embody.op('TDXNExt').module.SKIP_STORAGE_KEYS
        self.assertIn('_convoy_gen', skip,
                      'the reconcile generation counter changes on every '
                      'reinit -- serializing it is pure diff churn, the '
                      'same reason _watchdog_gen is excluded')

    def test_envoy_exposes_runtime_port(self):
        port = self.embody.ext.Envoy.RuntimePort()
        self.assertTrue(port is None or isinstance(port, int))
        # And it must be a real accessor, not a status-string parse (A-9).
        src = self.embody.op('EnvoyExt').text
        self.assertIn('def RuntimePort(self)', src)

    def test_parexec_wires_the_convoy_toggle(self):
        src = self.embody.op('parexec').text
        self.assertIn("par.name == 'Convoyenable'", src)
        self.assertIn('ConvoyExt.Register()', src)
        self.assertIn('ConvoyExt.Unregister()', src)
        self.assertIn("parent.Embody.op('convoy')", src)
        self.assertIn("par.Aiclient.eval() == 'none'", src)
        self.assertIn('parent.Embody.par.Envoyenable = True', src,
                      'Convoy-only mode must keep Envoy\'s internal relay on')
        self.assertIn('parent.Embody.par.Envoyenable = False', src,
                      'disabling Convoy must stop an unneeded internal relay')

    def test_execute_scrubs_and_unregisters(self):
        src = self.embody.op('execute').text
        self.assertIn("'Convoyenable'", src,
                      'init() must scrub the baked Convoyenable, exactly '
                      'as it does for Envoyenable')
        self.assertIn('Unregister(blocking=True', src,
                      'onExit() must clear this node port best-effort')

    def test_the_exit_callback_is_actually_armed(self):
        # The execute DAT shipped with start / create / projectpresave /
        # projectpostsave and NOT exit, so onExit() was dead code. Convoy's
        # unregister-on-exit is the first caller that needs it.
        dat = self.embody.op('execute')
        self.assertIsNotNone(dat)
        self.assertTrue(
            dat.par.exit.eval(),
            "the execute DAT's Exit callback must be ON, or onExit() -- "
            'and the Convoy unregister that rides it -- never runs')

    def test_client_module_is_resolved_exactly_once(self):
        # D4's named trap: mod.name is a LIVE DAT LOOKUP, so binding
        # `client = mod.convoy_client` inside a worker body is a threading
        # violation. The one legal reference lives in _client(), on the
        # main thread; the worker gets the captured module object.
        comp = self.embody.op('convoy')
        self.assertIsNotNone(comp)
        src = comp.op('ConvoyExt').text
        self.assertEqual(
            src.count('mod.convoy_client'), 1,
            'exactly one mod.convoy_client reference (inside _client), or '
            'the worker is re-resolving a DAT off the main thread')

    def test_convoy_uses_one_long_lived_threadmanager_task(self):
        src = self.embody.op('convoy').op('ConvoyExt').text
        self.assertIn('op.TDResources.ThreadManager', src)
        self.assertIn('self.ThreadManager.TDTask(', src)
        self.assertIn('standalone=True', src)
        self.assertIn('def _workerLoop(', src)
        self.assertNotIn('threading.Thread(', src,
                         'raw Python threads bypass TD ThreadManager')

    def test_sibling_worker_closures_cannot_reach_the_extension(self):
        method = self.embody.op('convoy').ext.ConvoyExt._submitSiblingApi
        function = getattr(method, '__func__', method)
        nested = {
            code.co_name: code for code in function.__code__.co_consts
            if isinstance(code, types.CodeType)
        }
        for name in ('_worker', '_progress', '_complete'):
            self.assertIn(name, nested)
            self.assertNotIn(
                'self', nested[name].co_freevars,
                '%s runs or is called from the worker and must not capture '
                'the TD-bound extension object' % name)

    def test_sibling_api_main_thread_gate_is_live(self):
        # _submitSiblingApi's wrong-thread gate does `import td` and asks
        # isMainThread(). Prove both halves are real inside TD, so the
        # ImportError fail-open can never be the branch that runs here --
        # a guard that silently assumes main is a dead guard.
        import td
        self.assertTrue(td.isMainThread())

    def test_worker_shutdown_is_generation_safe_and_non_blocking(self):
        src = self.embody.op('convoy').op('ConvoyExt').text
        self.assertIn("sys._convoy_workers", src)
        self.assertIn('old_event.set()', src)
        self.assertIn('self._stopWorker()', src)
        self.assertNotIn('_worker_thread.join(', src,
                         'extension reinit must never block joining a worker')

    def test_worker_loop_runs_plain_work_then_stops_on_sentinel(self):
        queue = Queue()
        shutdown = Event()
        seen = []
        queue.put(lambda: seen.append('ran'))
        queue.put(None)
        result = self.embody.op('convoy').ext.ConvoyExt._workerLoop(
            queue, shutdown, 17, idle_s=0.001)
        self.assertEqual(seen, ['ran'])
        self.assertEqual(result, {'generation': 17, 'stopped': True})

    def test_preset_shutdown_never_accepts_queued_work(self):
        queue = Queue()
        shutdown = Event()
        seen = []
        queue.put(lambda: seen.append('must not run'))
        shutdown.set()
        self.embody.op('convoy').ext.ConvoyExt._workerLoop(
            queue, shutdown, 18, idle_s=0.001)
        self.assertEqual(seen, [],
                         'a stale generation may finish current work but '
                         'must never accept the next queued callable')


class ConvoyExtBase(EmbodyTestCase):
    """Shared fixture. No network, no thread, no scheduler, no live pars."""

    def setUp(self):
        super().setUp()
        comp = self.embody.op('convoy')
        if comp is None:
            self.skipTest('the convoy child COMP is not in this project '
                          '(Stage B step 5 has not landed)')
        self.comp = comp
        self.convoy = comp.ext.ConvoyExt
        self.convoy_mod = comp.op('ConvoyExt').module
        self.client = StubClient(comp.op('convoy_client').module)

        self._patches = []
        self._runs = []
        self._logs = []
        self.status_writes = []
        self.id_writes = []
        self.enable_writes = []
        self.network_writes = []
        self.session = {}

        self._patch(self.convoy, '_client', lambda: self.client)
        self._patch(self.convoy, '_runInWorker',
                    lambda fn: (fn(), True)[1])
        self._patch(self.convoy, '_runBatchInWorkers',
                    self._runBatchSynchronously)
        self._patch(self.convoy_mod, 'run', self._fakeRun)
        self._patch(self.convoy, '_log',
                    lambda msg, level='INFO': self._logs.append((msg, level)))
        self._patch(self.convoy, '_status',
                    lambda text: self.status_writes.append(str(text)))
        self._patch(self.convoy, '_publishId',
                    lambda value: self.id_writes.append(str(value or '')))
        self._patch(self.convoy, '_setEnabled',
                    lambda value: self.enable_writes.append(bool(value)))
        self._patch(self.convoy, '_applyNetworkNodes',
                    lambda result: self.network_writes.append(result))
        self._patch(self.convoy, '_session', lambda: self.session)
        self._patch(self.convoy, '_enabled', lambda: True)
        self._patch(self.convoy, '_performing', lambda: False)
        self._patch(self.convoy, '_savedToe', lambda: 'C:/fake/project.toe')
        self._patch(self.convoy, '_readConvoyId', lambda: CONVOY_ID)
        self._patch(self.convoy, '_readBindingState', lambda: 'established')
        self._patch(self.convoy, '_readConsentScope', lambda: CONSENT_SCOPE)
        self._patch(self.convoy, '_envoyPort', lambda: 9870)
        self._patch(self.convoy, '_ensureWakeListener', lambda: True)
        self._patch(self.convoy, '_stopWakeListener', lambda: None)
        self._patch(self.convoy, '_remoteWakeEnabled', lambda: True)
        self._patch(self.convoy, '_performRequested', lambda: False)
        self._patch(self.convoy, '_wakeActive', lambda: False)
        self._patch(self.convoy, '_wakeEndpoint',
                    lambda: (47631, 'A' * 43))
        self._patch(self.convoy, '_wakeGrace', lambda: 60)
        # Instance bookkeeping the reconciler mutates.
        self._patch(self.convoy, '_busy', False)
        self._patch(self.convoy, '_result', None)
        self._patch(self.convoy, '_logged', '')
        self._patch(self.convoy, '_gen', 0)
        self._patch(self.convoy, '_tick_ms', self.convoy.TICK_MIN_MS)
        # Fresh bounded sibling API state for every test. The live extension
        # instance is shared by TestRunnerExt, so retaining one test's local
        # callbacks/results into the next would be both a leak and a race.
        self._patch(self.convoy, '_api_generation',
                    self.convoy._worker_generation)
        self._patch(self.convoy, '_api_requests', OrderedDict())
        self._patch(self.convoy, '_api_callbacks', {})
        self._patch(self.convoy, '_api_completion_events', Queue(
            maxsize=self.convoy.API_COMPLETION_MAX))
        self._patch(self.convoy, '_api_progress_events', Queue(
            maxsize=self.convoy.API_PROGRESS_MAX))
        self._patch(self.convoy, '_api_gate_event', Event())
        self._patch(self.convoy, '_api_poll_armed', False)

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
        """Patch by __dict__ presence, so restoring a patched CLASS method
        (or a staticmethod) deletes the instance shadow instead of leaving
        an unbound copy behind."""
        had = name in getattr(obj, '__dict__', {})
        old = obj.__dict__.get(name) if had else None
        setattr(obj, name, value)
        self._patches.append((obj, name, old, had))

    def _fakeRun(self, *a, **kw):
        """Record every scheduled call; DISPATCH only the initial poll.

        Dispatching the poll armed by _beginCall (attempts == 0) lets a
        whole register round trip finish inside one synchronous test
        method. Retries (attempts > 0) and tick reschedules are recorded
        only -- dispatching those would recurse forever.
        """
        self._runs.append((a, kw))
        if not (a and isinstance(a[0], str) and '_pollCall' in a[0]):
            return
        try:
            ext, action, gen, attempts = a[1], a[2], a[3], a[4]
        except IndexError:
            return
        if attempts == 0:
            ext._pollCall(action, gen, attempts)

    def _runBatchSynchronously(self, client, context, request, progress,
                               complete, gate_event):
        """No-thread default for ordinary API tests.

        It invokes the shipping preflight, target and collector code through
        the module's generic worker entry. Dedicated scale tests below restore
        the real ThreadManager fanout method.
        """
        try:
            result = self.convoy_mod._run_sibling_api_request(
                client, 'batch', context, request, progress, gate_event)
        except Exception as e:
            result = {
                'state': 'error', 'ok': False,
                'reason': 'worker_exception',
                'detail': '%s: %s' % (type(e).__name__, e),
            }
        complete(result)
        return True

    def _useRealBatchFanout(self):
        """Restore production fanout against a controllable manager."""
        manager = _ThreadManagerHarness()
        method = self.convoy_mod.ConvoyExt._runBatchInWorkers.__get__(
            self.convoy, self.convoy_mod.ConvoyExt)
        self._patch(self.convoy, '_runBatchInWorkers', method)
        self._patch(self.convoy, 'ThreadManager', manager)
        self._patch(self.convoy, '_runInWorker', manager.enqueue_callable)
        return manager

    # -- helpers ------------------------------------------------------
    def _warnings(self):
        return [m for m, level in self._logs if level == 'WARNING']

    def _tickReschedules(self):
        return [a for a, _kw in self._runs
                if a and isinstance(a[0], str) and '_convoyTick' in a[0]]


class TestNetworkStatusProjection(ConvoyExtBase):

    def test_online_and_cached_nodes_have_honest_ui_values(self):
        result = {
            'state': 'nodes',
            'nodes': [
                {
                    'node_id': '1' * 32, 'host_id': 'a' * 32,
                    'node_name': 'render-a / show', 'ip': '10.0.0.2',
                    'status': 'online', 'online': True,
                    'embody_version': '6.0.178',
                    'touchdesigner_version': '2025.32180',
                    'controller_count': 2,
                    'last_seen_age_s': 8.9,
                },
                {
                    'node_id': '2' * 32, 'host_id': 'b' * 32,
                    'hostname': 'render-b', 'ip': '10.0.0.3',
                    'status': 'offline', 'online': False,
                    'controller_count': 1,
                    'last_seen_age_s': 125,
                },
            ],
        }
        rows = self.convoy._nodeStatusRows(result)
        self.assertLen(rows, 2)
        # Deliberately minimal: 4 columns only (Node Name, IP, Status, Last
        # Seen). Controllers / Details / Embody Version were removed.
        self.assertEqual(set(rows[0]),
                         {'Nodename', 'Ipaddress', 'Nodestatus', 'Lastseen'})
        self.assertEqual(rows[0]['Nodename'], 'render-a / show')
        self.assertEqual(rows[0]['Ipaddress'], '10.0.0.2')
        self.assertEqual(rows[0]['Nodestatus'], 'Online')
        self.assertEqual(rows[0]['Lastseen'], '8s ago')
        self.assertEqual(rows[1]['Nodename'], 'render-b')
        self.assertEqual(rows[1]['Nodestatus'], 'Offline')
        self.assertEqual(rows[1]['Lastseen'], '2m ago')

    def test_a_traveled_stamp_cannot_mask_the_real_hostname(self):
        """A node_name stamped on another machine travels inside the .toe;
        a whole fleet read 'TEC-A4D / Render.36' (2026-08-19). The row's
        live hostname is prefixed whenever the name does not contain it,
        so nodes stay tellable-apart even before the stamp heals."""
        rows = self.convoy._nodeStatusRows({
            'state': 'nodes',
            'nodes': [
                {'node_id': '1' * 32, 'host_id': 'a' * 32,
                 'hostname': 'INF-FLEX-2',
                 'node_name': 'TEC-A4D / Render.36', 'ip': '10.0.0.4',
                 'status': 'online', 'online': True,
                 'last_seen_age_s': 3.0},
                {'node_id': '2' * 32, 'host_id': 'b' * 32,
                 'hostname': 'INF-FLEX-3',
                 'node_name': 'INF-FLEX-3 / Render', 'ip': '10.0.0.5',
                 'status': 'online', 'online': True,
                 'last_seen_age_s': 3.0},
            ],
        })
        self.assertEqual(rows[0]['Nodename'],
                         'INF-FLEX-2 (TEC-A4D / Render.36)')
        self.assertEqual(rows[1]['Nodename'], 'INF-FLEX-3 / Render',
                         'a name already carrying its hostname is not '
                         'double-prefixed')

    def test_last_seen_without_an_age_reads_never(self):
        """'Unavailable' read like an error state (field 2026-08-19);
        'Never' says what is known. Online rows still read 'Now'."""
        self.assertEqual(self.convoy._lastSeenText(None, False), 'Never')
        self.assertEqual(self.convoy._lastSeenText(None, True), 'Now')

    def test_a_failed_directory_read_means_leave_existing_rows(self):
        for value in (None, {}, {'state': 'unreachable'},
                      {'state': 'host_error'}):
            self.assertIsNone(self.convoy._nodeStatusRows(value))

    def test_last_seen_ages_between_directory_fetches(self):
        """The column is a RELATIVE time, but the directory is only refetched
        on the 30s heartbeat -- so writing it once and leaving it made the page
        show "15s ago" minutes later. The tick must age it with no new data."""
        import time as _t
        self.convoy._node_ages = [(5.0, True), (None, False)]
        self.convoy._node_ages_at = _t.time() - 120.0   # data is 2 min old
        self.convoy._projectNodeRows([
            {'Nodename': 'a', 'Ipaddress': '1', 'Nodestatus': 'Online',
             'Lastseen': '5s ago'},
            {'Nodename': 'b', 'Ipaddress': '2', 'Nodestatus': 'Offline',
             'Lastseen': 'Unavailable'}])
        self.convoy._refreshLastSeen()
        seq = self.convoy._sequenceByName(self.embody, 'Convoynodes')
        if seq is None or not seq.numBlocks:
            self.skipTest('the Convoy Nodes sequence is not on this build')
        first = self.convoy._sequenceBlockPar(
            self.embody, seq, list(seq.blocks)[0], 1, 'Lastseen')
        self.assertIsNotNone(first)
        self.assertEqual(
            first.eval(), '2m ago',
            'a 5s age captured 2 minutes ago must now read 2m ago, not 5s')

    def test_node_status_rows_are_bounded_plain_values(self):
        rows = self.convoy._nodeStatusRows({
            'state': 'nodes',
            'nodes': [{
                'node_id': 'n' * 1000, 'host_id': 'h' * 1000,
                'node_name': 'x' * 2000, 'ip': '1' * 1000,
                'status': 'online' * 100, 'online': True,
                'embody_version': 'v' * 500,
                'touchdesigner_version': 't' * 500,
            }],
        })
        self.assertLen(rows, 1)
        self.assertEqual(set(rows[0]),
                         {'Nodename', 'Ipaddress', 'Nodestatus', 'Lastseen'})
        self.assertLessEqual(len(rows[0]['Nodename']), 512)
        self.assertLessEqual(len(rows[0]['Ipaddress']), 255)
        self.assertLessEqual(len(rows[0]['Nodestatus']), 70)


class TestReconcileCalls(ConvoyExtBase):
    """The whole point of D2: one mechanism, no redundant network calls."""

    def test_first_pass_registers_once(self):
        self.convoy._reconcile()
        self.assertEqual(self.client.count('register'), 1)
        payload = self.client.last('register')
        self.assertEqual(payload['convoy_id'], CONVOY_ID)
        self.assertEqual(payload['comp_path'], self.embody.path)
        self.assertEqual(payload['envoy_port'], 9870)
        self.assertTrue(payload['envoy_ready'])
        self.assertTrue(payload['remote_wake'])
        self.assertFalse(payload['perform_mode'])
        self.assertFalse(payload['wake_active'])
        self.assertNotIn('wake_port', payload,
                         'a wake-only endpoint is routable only while Perform '
                         'Mode makes remote wake applicable')
        self.assertTrue(payload['wake_pending'])
        self.assertEqual(payload['wake_grace_s'], 60)
        self.assertTrue(re.match(r'^nd_[0-9a-f]{32}$',
                                 payload['node_discriminator']))
        metadata = payload['metadata']
        self.assertEqual(metadata['toe_path'], 'C:/fake/project.toe')
        self.assertEqual(metadata['toe_name'], 'project.toe')
        self.assertEqual(metadata['process_id'], os.getpid())
        # Node name comes from the Convoynodename parameter, which auto-
        # populates to "hostname / toe-name" via a machine-independent
        # expression (or a user override). It is the source of truth, so the
        # registered name matches the parameter's evaluated value.
        self.assertEqual(metadata['node_name'],
                         self.embody.par.Convoynodename.eval())
        self.assertTrue(payload['runtime_id'].startswith('rt_'),
                        'runtime_id is REQUIRED on every call: the host '
                        're-mints the run identity when it is omitted')
        self.assertTrue(self.session.get('registered'))
        self.assertEqual(self.session.get('node_id'), NODE_ID)
        self.assertEqual(self.client.count('network_nodes'), 1)
        self.assertEqual(self.client.last('network_nodes')['convoy_id'],
                         CONVOY_ID)
        self.assertLen(self.network_writes, 1)
        self.assertEqual(self.network_writes[0]['state'], 'nodes')

    def test_unchanged_tuple_issues_zero_calls(self):
        self.convoy._reconcile()
        settled = len(self.client.calls)
        self.convoy._reconcile()
        self.convoy._reconcile()
        self.assertEqual(len(self.client.calls), settled,
                         'an unchanged desired-state tuple inside the '
                         'heartbeat window must not touch the network')

    def test_changed_envoy_port_re_registers_with_the_same_runtime_id(self):
        self.convoy._reconcile()
        first = self.client.last('register')['runtime_id']
        self._patch(self.convoy, '_envoyPort', lambda: 9871)
        self.convoy._reconcile()
        self.assertEqual(self.client.count('register'), 2)
        second = self.client.last('register')
        self.assertEqual(second['envoy_port'], 9871)
        self.assertEqual(
            second['runtime_id'], first,
            'runtime_id is minted ONCE per launch -- a heartbeat that '
            'changes it invalidates every in-flight expected_runtime_id')

    def test_enable_then_disable_drives_one_register_and_one_unregister(self):
        self.convoy._reconcile()
        self.assertEqual(self.client.count('register'), 1)
        self._patch(self.convoy, '_enabled', lambda: False)
        self.convoy._reconcile()
        self.assertEqual(self.client.count('unregister'), 1)
        sent = self.client.last('unregister')
        self.assertEqual(sent['node_id'], NODE_ID)
        self.assertEqual(sent['reason'], 'disabled')
        self.assertTrue(
            sent['runtime_id'],
            'the runtime_id is the ownership proof -- without it a '
            'departing session zeroes a surviving one\'s live port')
        self.convoy._reconcile()
        self.convoy._reconcile()
        self.assertEqual(self.client.count('unregister'), 1,
                         'a disabled, already-unregistered node is silent')
        self.assertEqual(self.status_writes[-1], 'Disabled')

    def test_convoy_status_snapshot_is_total(self):
        self.convoy._reconcile()
        snap = self.convoy.ConvoyStatus()
        for key in ('enabled', 'performing', 'perform_mode_requested',
                    'wake_active', 'remote_wake', 'wake_port',
                    'saved_project', 'convoy_id',
                    'node_id', 'host_id', 'runtime_id', 'registered',
                    'envoy_port', 'busy', 'api_pending', 'api_results',
                    'status'):
            self.assertDictHasKey(snap, key)
        self.assertNotIn('error', snap, 'the snapshot must never raise')
        self.assertEqual(snap['node_id'], NODE_ID)
        self.assertEqual(snap['convoy_id'], CONVOY_ID)
        self.assertTrue(snap['registered'])

    def test_td_exit_is_sent_as_shutdown_not_membership_disable(self):
        self.convoy._reconcile()
        self.convoy.Unregister(blocking=False, reason='TD exit')
        sent = self.client.last('unregister')
        self.assertEqual(sent['reason'], 'shutdown')

    def test_invalid_unregister_intent_fails_closed(self):
        self.convoy._reconcile()
        before = self.client.count('unregister')
        result = self.convoy.Unregister(blocking=False,
                                        reason='remote-request')
        self.assertEqual(result['reason'], 'invalid_unregister_reason')
        self.assertEqual(self.client.count('unregister'), before)

    def test_threadmanager_capacity_failure_is_immediate_and_honest(self):
        self._patch(self.convoy, '_runInWorker', lambda fn: False)
        self.convoy._reconcile()
        self.assertStartsWith(self.status_writes[-1], 'Error:')
        self.assertFalse(self.convoy._busy)
        self.assertEqual(self.client.count('register'), 0,
                         'a rejected TDTask never ran the worker body')

    def test_registered_without_a_port_keeps_converging(self):
        self.client.register_result = {'state': 'registered',
                                       'node_id': NODE_ID,
                                       'host_id': HOST_ID, 'envoy_port': None}
        self._patch(self.convoy, '_envoyPort', lambda: None)
        self.convoy._reconcile()
        self.assertEqual(self.status_writes[-1],
                         'Registered -- Envoy port pending')
        self.assertLessEqual(self.convoy._tick_ms, self.convoy.TICK_MIN_MS,
                             'a portless registration is NOT steady state')


class TestAutomaticRealmAdoption(ConvoyExtBase):
    """The host's converged realm must be persisted before steady state."""

    def setUp(self):
        super().setUp()
        self.local_id = CONVOY_ID
        self.local_state = 'candidate'
        self.authoritative_id = 'cv_' + '1' * 16
        self.adoptions = []
        self._patch(self.convoy, '_readConvoyId', lambda: self.local_id)
        self._patch(self.convoy, '_readBindingState',
                    lambda: self.local_state)

        def _adopt(new_id, expected_id, binding_state):
            self.adoptions.append((new_id, expected_id, binding_state))
            if expected_id != self.local_id:
                return ''
            self.local_id = new_id
            self.local_state = binding_state
            return new_id

        self._patch(self.embody.ext.Embody, '_adoptConvoyId', _adopt)

    def test_registration_adopts_host_authority_and_retries_immediately(self):
        self.client.register_result.update({
            'convoy_id': self.authoritative_id,
            'realm_state': 'established',
        })
        self.convoy._reconcile()

        self.assertEqual(self.adoptions, [(
            self.authoritative_id, CONVOY_ID, 'established')])
        self.assertEqual(self.id_writes[-1], self.authoritative_id)
        self.assertTrue(self.session.get('registered'))
        self.assertIsNone(self.session.get('sent'),
                          'the stale candidate tuple must not become steady')
        self.assertLessEqual(self.session.get('next_call_at'),
                             self.convoy_mod.time.monotonic())
        self.assertEqual(self.client.last('network_nodes')['convoy_id'],
                         self.authoritative_id)

    def test_failed_project_cas_never_claims_registration_succeeded(self):
        self.client.register_result.update({
            'convoy_id': self.authoritative_id,
            'realm_state': 'established',
        })
        self._patch(self.embody.ext.Embody, '_adoptConvoyId',
                    lambda *args: '')
        self.convoy._reconcile()

        self.assertFalse(self.session.get('registered'))
        self.assertIsNone(self.session.get('sent'))
        self.assertStartsWith(self.status_writes[-1], 'Error:')
        self.assertTrue(any('project.json' in message.lower()
                            for message, _level in self._logs))


class TestSiblingAPI(ConvoyExtBase):
    """TD-originated relay is async, exact, bounded and main-thread applied."""

    def setUp(self):
        super().setUp()
        self.session.update({
            'registered': True,
            'host_id': HOST_ID,
            'node_id': NODE_ID,
            'runtime_id': 'rt_source',
        })

    def _poll(self):
        self.convoy._pollApiEvents(self.convoy._api_generation)

    def test_list_nodes_returns_before_main_thread_callback(self):
        callbacks = []
        handle = self.convoy.listNodes(callback=callbacks.append)

        self.assertEqual(handle['state'], 'queued')
        self.assertEqual(callbacks, [],
                         'a worker completion must not call TD code inline')
        self.assertEqual(
            self.convoy.requestResult(handle)['state'], 'queued')
        self._poll()

        result = self.convoy.requestResult(handle)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['result']['state'], 'nodes')
        self.assertFalse(result['result']['wakes_touchdesigner'])
        self.assertLen(callbacks, 1)
        self.assertEqual(callbacks[0]['event'], 'complete')
        self.assertEqual(callbacks[0]['source']['node_id'], NODE_ID)

    def test_call_routes_exact_target_with_non_spoofable_source(self):
        arguments = {'path': '/project1'}
        handle = self.convoy.call(
            OTHER_HOST_ID, 'z' * 32, 'query_network', arguments,
            expected_runtime_id='rt_target', timeout_s=12)
        arguments['path'] = '/mutated-after-submit'
        self._poll()

        sent = self.client.last('submit_sibling_call')
        self.assertEqual(sent['target_host_id'], OTHER_HOST_ID)
        self.assertEqual(sent['target_node_id'], 'z' * 32)
        self.assertEqual(sent['convoy_id'], CONVOY_ID)
        self.assertEqual(sent['expected_runtime_id'], 'rt_target')
        self.assertEqual(sent['arguments'], {'path': '/project1'})
        self.assertEqual(sent['controller_id'],
                         'td:%s:%s:rt_source' % (HOST_ID, NODE_ID))
        self.assertEqual(sent['idempotency_key'], handle['request_id'])
        result = self.convoy.requestResult(handle)
        self.assertEqual(result['target']['host_id'], OTHER_HOST_ID)
        self.assertTrue(result['result']['wakes_touchdesigner'])

    def test_disabled_candidate_and_malformed_requests_never_probe(self):
        self._patch(self.convoy, '_enabled', lambda: False)
        disabled = self.convoy.listNodes()
        self._poll()
        self.assertEqual(self.convoy.requestResult(disabled)['result']['reason'],
                         'convoy_disabled')
        self.assertEqual(self.client.count('probe'), 0)

        self._patch(self.convoy, '_enabled', lambda: True)
        self._patch(self.convoy, '_readBindingState', lambda: 'candidate')
        candidate = self.convoy.ping(HOST_ID, NODE_ID)
        self._poll()
        self.assertEqual(
            self.convoy.requestResult(candidate)['result']['reason'],
            'convoy_not_established')
        self.assertEqual(self.client.count('probe'), 0)

        self._patch(self.convoy, '_readBindingState', lambda: 'established')
        malformed = self.convoy.call('', NODE_ID, 'query_network', {})
        self._poll()
        self.assertEqual(
            self.convoy.requestResult(malformed)['result']['reason'],
            'invalid_arguments')
        self.assertEqual(self.client.count('probe'), 0)

        too_long = self.convoy.call(
            HOST_ID, NODE_ID, 'query_network', {}, timeout_s=61)
        self._poll()
        self.assertEqual(
            self.convoy.requestResult(too_long)['result']['reason'],
            'invalid_arguments')
        self.assertEqual(self.client.count('probe'), 0)

    def test_ping_waits_for_host_native_verdict_without_waking_td(self):
        callbacks = []
        clock = iter((100.0, 104.0)).__next__
        self._patch(self.convoy_mod.time, 'monotonic', clock)
        handle = self.convoy.ping(
            OTHER_HOST_ID, 'p' * 32, timeout_s=7,
            callback=callbacks.append)
        self.assertEqual(callbacks, [])
        self._poll()

        sent = self.client.last('submit_sibling_call')
        self.assertEqual(sent['operation'], 'convoy_ping')
        self.assertEqual(sent['arguments'], {})
        self.assertEqual(self.client.count('wait_sibling_job'), 1)
        self.assertEqual(
            self.client.last('wait_sibling_job')['timeout_s'], 3.0,
            'submission and wait must share one total deadline')
        result = self.convoy.requestResult(handle)
        self.assertEqual(result['result']['job']['state'], 'succeeded')
        self.assertFalse(result['result']['wakes_touchdesigner'])
        self.assertEqual(callbacks[-1]['event'], 'complete')
        self.assertTrue(all(
            not item['result']['wakes_touchdesigner']
            for item in callbacks if item.get('result')))

    def test_get_and_federated_cancel_are_exact_and_non_waking(self):
        got = self.convoy.getJob(
            OTHER_HOST_ID, 'cj_remote', since=3.5, timeout_s=6)
        cancelled = self.convoy.cancelJob(
            OTHER_HOST_ID, 'cj_remote', timeout_s=8)
        self._poll()

        get_call = self.client.last('get_sibling_job')
        self.assertEqual(get_call, {
            'target_host_id': OTHER_HOST_ID, 'convoy_id': CONVOY_ID,
            'delivery_id': 'cj_remote', 'since': 3.5, 'timeout': 6.0,
        })
        cancel_call = self.client.last('cancel_sibling_job')
        self.assertEqual(cancel_call, {
            'target_host_id': OTHER_HOST_ID, 'convoy_id': CONVOY_ID,
            'delivery_id': 'cj_remote', 'timeout': 8.0,
        })
        self.assertFalse(
            self.convoy.requestResult(got)['result']['wakes_touchdesigner'])
        self.assertFalse(self.convoy.requestResult(
            cancelled)['result']['wakes_touchdesigner'])

    def test_batch_has_one_explicit_result_per_target_and_is_not_atomic(self):
        targets = [
            {'host_id': HOST_ID, 'node_id': NODE_ID},
            {'host_id': OTHER_HOST_ID, 'node_id': 'b' * 32,
             'expected_runtime_id': 'rt_b'},
        ]
        operations = [
            {'tool': 'query_network', 'params': {'root': '/'}},
            {'tool': 'get_op_errors', 'params': {'op_path': '/project1'}},
        ]
        handle = self.convoy.batch(targets, operations)
        self._poll()

        calls = [payload for name, payload in self.client.calls
                 if name == 'submit_sibling_call']
        self.assertLen(calls, 2)
        self.assertEqual([row['target_host_id'] for row in calls],
                         [HOST_ID, OTHER_HOST_ID])
        self.assertTrue(all(row['operation'] == 'batch_operations'
                            for row in calls))
        self.assertEqual(calls[0]['arguments'], {'operations': operations})
        self.assertEqual(calls[1]['expected_runtime_id'], 'rt_b')
        result = self.convoy.requestResult(handle)['result']
        self.assertFalse(result['atomic'])
        self.assertFalse(result['partial'])
        self.assertLen(result['results'], 2)

    def test_batch_enforces_cumulative_result_budget_in_the_worker(self):
        self._patch(self.convoy, 'API_RESULT_MAX_BYTES', 128 * 1024)
        self.client.sibling_submit_result['job']['result'] = {
            'blob': 'x' * (80 * 1024)}
        handle = self.convoy.batch([
            {'host_id': HOST_ID, 'node_id': NODE_ID},
            {'host_id': OTHER_HOST_ID, 'node_id': 'b' * 32},
        ], [{'tool': 'query_network', 'params': {}}])
        self._poll()

        result = self.convoy.requestResult(handle)['result']
        self.assertEqual(result['state'], 'batch')
        self.assertTrue(any(
            row['result'].get('reason') == 'batch_result_budget_exceeded'
            for row in result['results']))
        self.assertNotEqual(result.get('reason'), 'result_too_large')

    def test_batch_targets_overlap_and_terminal_rows_keep_input_order(self):
        manager = self._useRealBatchFanout()
        self._patch(self.convoy, 'API_BATCH_WORKER_MAX', 4)
        hosts = [character * 32 for character in 'abcd']
        indexes = {host: index for index, host in enumerate(hosts)}
        releases = [Event() for _ in hosts]
        finished = [Event() for _ in hosts]
        all_started = Event()
        lock = Lock()
        state = {'active': 0, 'maximum': 0, 'started': 0,
                 'completion_order': []}

        def _submit(handle, target_host_id, convoy_id, target_node_id,
                    controller_id, operation, arguments=None, **kwargs):
            index = indexes[target_host_id]
            with lock:
                state['active'] += 1
                state['maximum'] = max(state['maximum'], state['active'])
                state['started'] += 1
                if state['started'] == len(hosts):
                    all_started.set()
            releases[index].wait()
            with lock:
                state['completion_order'].append(index)
                state['active'] -= 1
            finished[index].set()
            return {
                'state': 'accepted' if index != 1 else 'refused',
                'ok': index != 1,
                'marker': index,
                'reason': None if index != 1 else 'target_refused',
                'job': {'delivery_id': 'cj_%d' % index,
                        'state': 'queued'},
            }

        self._patch(self.client, 'submit_sibling_call', _submit)
        handle = self.convoy.batch([
            {'host_id': host, 'node_id': str(index) * 32}
            for index, host in enumerate(hosts)
        ], [{'tool': 'query_network', 'params': {}}], timeout_s=5)

        self.assertTrue(all_started.wait(1.0),
                        'all four targets should be in flight together')
        for index in reversed(range(len(hosts))):
            releases[index].set()
            self.assertTrue(finished[index].wait(1.0))
        self.assertTrue(manager.join_all())
        self.assertEqual(manager.errors, [])
        self._poll()

        result = self.convoy.requestResult(handle)['result']
        self.assertEqual(state['maximum'], 4)
        self.assertEqual(state['completion_order'], [3, 2, 1, 0],
                         'the stub deliberately completed out of order')
        self.assertEqual([row['index'] for row in result['results']],
                         [0, 1, 2, 3])
        self.assertEqual([row['target']['host_id']
                          for row in result['results']], hosts)
        self.assertEqual([row['result']['marker']
                          for row in result['results']], [0, 1, 2, 3])
        self.assertTrue(result['partial'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['results'][1]['result']['reason'],
                         'target_refused')

    def test_batch_fanout_has_a_hard_worker_bound(self):
        manager = self._useRealBatchFanout()
        self._patch(self.convoy, 'API_BATCH_WORKER_MAX', 3)
        release = Event()
        first_wave = Event()
        lock = Lock()
        state = {'active': 0, 'maximum': 0, 'started': []}

        def _submit(handle, target_host_id, convoy_id, target_node_id,
                    controller_id, operation, arguments=None, **kwargs):
            with lock:
                state['active'] += 1
                state['maximum'] = max(state['maximum'], state['active'])
                state['started'].append(target_host_id)
                if len(state['started']) == 3:
                    first_wave.set()
            release.wait()
            with lock:
                state['active'] -= 1
            return {'state': 'accepted', 'ok': True,
                    'job': {'delivery_id': 'cj_' + target_host_id[:1],
                            'state': 'queued'}}

        self._patch(self.client, 'submit_sibling_call', _submit)
        hosts = [chr(ord('a') + index) * 32 for index in range(9)]
        handle = self.convoy.batch([
            {'host_id': host, 'node_id': str(index) * 32}
            for index, host in enumerate(hosts)
        ], [{'tool': 'query_network', 'params': {}}], timeout_s=5)

        self.assertTrue(first_wave.wait(1.0))
        with lock:
            self.assertEqual(len(state['started']), 3,
                             'a fourth target must remain queued')
        release.set()
        self.assertTrue(manager.join_all())
        self.assertEqual(manager.errors, [])
        self._poll()

        result = self.convoy.requestResult(handle)['result']
        self.assertEqual(state['maximum'], 3)
        self.assertEqual(len(state['started']), 9)
        self.assertEqual(result['count'], 9)
        self.assertFalse(result['partial'])

    def test_slow_target_does_not_block_peers_or_extend_total_timeout(self):
        manager = self._useRealBatchFanout()
        self._patch(self.convoy, 'API_BATCH_WORKER_MAX', 2)
        slow_host = 's' * 32
        fast_hosts = [character * 32 for character in 'fgh']
        release_slow = Event()
        slow_started = Event()
        fast_complete = Event()
        lock = Lock()
        seen_fast = []
        seen_timeouts = []

        def _submit(handle, target_host_id, convoy_id, target_node_id,
                    controller_id, operation, arguments=None, **kwargs):
            with lock:
                seen_timeouts.append(float(kwargs['timeout_s']))
            if target_host_id == slow_host:
                slow_started.set()
                # Deliberately emulate a broken transport that ignores its
                # own timeout. The coordinator must still honor the batch's
                # absolute deadline and publish peer results.
                release_slow.wait()
                return {'state': 'accepted', 'ok': True,
                        'marker': 'late',
                        'job': {'delivery_id': 'cj_slow'}}
            with lock:
                seen_fast.append(target_host_id)
                if len(seen_fast) == len(fast_hosts):
                    fast_complete.set()
            return {'state': 'accepted', 'ok': True,
                    'marker': target_host_id[:1],
                    'job': {'delivery_id': 'cj_' + target_host_id[:1]}}

        self._patch(self.client, 'submit_sibling_call', _submit)
        targets = [{'host_id': slow_host, 'node_id': '0' * 32}]
        targets.extend({
            'host_id': host, 'node_id': str(index + 1) * 32
        } for index, host in enumerate(fast_hosts))
        started_at = time.monotonic()
        handle = self.convoy.batch(
            targets, [{'tool': 'query_network', 'params': {}}],
            timeout_s=0.2)

        self.assertTrue(slow_started.wait(1.0))
        self.assertTrue(fast_complete.wait(1.0),
                        'the free worker must serve peers while one hangs')
        coordinator = manager.threads[-1]
        coordinator.join(1.0)
        elapsed = time.monotonic() - started_at
        self.assertFalse(coordinator.is_alive(),
                         'one cumulative deadline must finish aggregation')
        self.assertLess(elapsed, 0.75,
                        'timeout must not repeat once per target or worker')
        self._poll()

        result = self.convoy.requestResult(handle)['result']
        self.assertEqual(result['results'][0]['result']['reason'],
                         'batch_timeout')
        self.assertEqual([row['result'].get('marker')
                          for row in result['results'][1:]], ['f', 'g', 'h'])
        self.assertTrue(result['partial'])
        self.assertEqual(set(seen_fast), set(fast_hosts))
        self.assertTrue(all(0.0 < value <= 0.2
                            for value in seen_timeouts))

        release_slow.set()
        self.assertTrue(manager.join_all())
        self.assertEqual(manager.errors, [])

    def test_old_generation_event_cannot_complete_a_new_request(self):
        handle = self.convoy.listNodes()
        while not self.convoy._api_progress_events.empty():
            self.convoy._api_progress_events.get_nowait()
        while not self.convoy._api_completion_events.empty():
            self.convoy._api_completion_events.get_nowait()
        generation = self.convoy._api_generation
        self.convoy._api_completion_events.put_nowait({
            'generation': generation - 1,
            'request_id': handle['request_id'],
            'result': {'state': 'nodes', 'nodes': []},
        })
        self._poll()
        self.assertEqual(self.convoy.requestResult(handle)['state'], 'queued')

        self.convoy._api_completion_events.put_nowait({
            'generation': generation,
            'request_id': handle['request_id'],
            'result': {'state': 'nodes', 'nodes': [],
                       'wakes_touchdesigner': False},
        })
        self._poll()
        self.assertEqual(
            self.convoy.requestResult(handle)['state'], 'completed')

    def test_result_limit_replaces_oversized_payload_and_lookup_is_detached(self):
        self.client.network_result['oversized'] = 'x' * 4096
        self._patch(self.convoy, 'API_RESULT_MAX_BYTES', 1024)
        handle = self.convoy.listNodes()
        self._poll()
        first = self.convoy.requestResult(handle)
        self.assertEqual(first['state'], 'failed')
        self.assertEqual(first['result']['reason'], 'result_too_large')

        first['result']['reason'] = 'caller-mutated'
        again = self.convoy.requestResult(handle)
        self.assertEqual(again['result']['reason'], 'result_too_large')
        consumed = self.convoy.requestResult(handle, consume=True)
        self.assertEqual(consumed['request_id'], handle['request_id'])
        self.assertIsNone(self.convoy.requestResult(handle))

    def test_threadmanager_rejection_completes_honestly(self):
        self._patch(self.convoy, '_runInWorker', lambda fn: False)
        handle = self.convoy.call(
            OTHER_HOST_ID, NODE_ID, 'query_network', {})
        self._poll()
        result = self.convoy.requestResult(handle)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['reason'],
                         'thread_manager_unavailable')
        self.assertEqual(self.client.count('submit_sibling_call'), 0)

    def test_disable_revokes_a_worker_still_waiting_in_the_queue(self):
        queued = []
        self._patch(self.convoy, '_runInWorker',
                    lambda fn: (queued.append(fn), True)[1])
        handle = self.convoy.call(
            OTHER_HOST_ID, NODE_ID, 'query_network', {})
        self.assertLen(queued, 1)

        self.convoy._revokeSiblingApi()
        queued[0]()
        self._poll()
        result = self.convoy.requestResult(handle)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['reason'], 'convoy_disabled')
        self.assertEqual(self.client.count('probe'), 0)

    def test_request_capacity_never_evicts_an_inflight_handle(self):
        queued = []
        self._patch(self.convoy, 'API_REQUEST_MAX', 2)
        self._patch(self.convoy, '_runInWorker',
                    lambda fn: (queued.append(fn), True)[1])
        first = self.convoy.listNodes()
        second = self.convoy.listNodes()
        overflow = self.convoy.listNodes()

        self.assertEqual(first['state'], 'queued')
        self.assertEqual(second['state'], 'queued')
        self.assertEqual(overflow['state'], 'failed')
        self.assertEqual(overflow['result']['reason'], 'request_capacity')
        self.assertIsNotNone(self.convoy.requestResult(first))
        self.assertIsNotNone(self.convoy.requestResult(second))
        self.assertIsNone(self.convoy.requestResult(overflow))
        self.assertLen(queued, 2)


class TestQuietRefusals(ConvoyExtBase):
    """Absence, Perform Mode and an unsaved project are not failures."""

    def test_perform_mode_keeps_only_membership_and_wake_heartbeat_alive(self):
        self._patch(self.convoy, '_performing', lambda: True)
        self._patch(self.convoy, '_performRequested', lambda: True)
        self._patch(self.convoy, '_envoyPort', lambda: None)
        self.convoy._reconcile()
        self.assertEqual(self.client.count('register'), 1)
        payload = self.client.last('register')
        self.assertTrue(payload['perform_mode'])
        self.assertFalse(payload['wake_active'])
        self.assertFalse(payload['envoy_ready'])
        self.assertNotIn('envoy_port', payload)
        self.assertEqual(payload['wake_port'], 47631)
        self.assertEqual(self.status_writes, [],
                         'Perform Mode must not clobber the show readout')
        # Even a result landing from a call that started BEFORE the show.
        self.convoy._apply({'state': 'registered', 'node_id': NODE_ID,
                            'host_id': HOST_ID, 'envoy_port': 9870},
                           self.client)
        self.assertEqual(self.status_writes, [])

    def test_untitled_project_refuses_to_register(self):
        self._patch(self.convoy, '_savedToe', lambda: None)
        self.convoy._reconcile()
        self.assertEqual(self.client.calls, [],
                         'a never-saved project must not mint a node record')
        self.assertEqual(self.status_writes[-1], 'Waiting for project save')
        self.assertEqual(self._warnings(), [])

    def test_missing_convoy_id_never_mints_on_a_tick(self):
        self._patch(self.convoy, '_readConvoyId', lambda: '')
        self.convoy._reconcile()
        self.assertEqual(self.client.calls, [],
                         'minting is gated on an EXPLICIT enable (A-13); a '
                         'tick must never write a tracked file or prompt')
        self.assertStartsWith(self.status_writes[-1], 'Error:')
        self.assertEqual(self.id_writes, [])

    def test_absent_host_is_quiet(self):
        self.client.probe_result = self.client.absent()
        self.convoy._reconcile()
        self.assertEqual(self.client.count('register'), 0)
        self.assertEqual(self.status_writes[-1], 'No Convoy host app')
        self.assertEqual(self._warnings(), [],
                         'no host app is the normal state of almost every '
                         'install -- it must never warn')
        # And it repeats without re-logging.
        before = len(self._logs)
        self.session['next_call_at'] = None
        self.convoy._reconcile()
        self.assertEqual(len(self._logs), before,
                         'one line on the transition only')

    def test_stale_host_reads_as_stale_not_error(self):
        self.client.probe_result = self.client.stale()
        self.convoy._reconcile()
        self.assertEqual(self.status_writes[-1], 'Host app stale')
        self.assertEqual(self._warnings(), [])

    def test_a_refusal_is_reported_as_a_decision(self):
        self.client.register_result = {'ok': False, 'state': 'refused',
                                       'reason': 'node_identity_conflict',
                                       'detail': 'two roots, one node'}
        self.convoy._reconcile()
        self.assertEqual(self.status_writes[-1],
                         'Refused: node_identity_conflict')
        self.assertLen(self._warnings(), 1)

    def test_a_vanished_host_warns_once_and_backs_off(self):
        self.client.register_result = {'state': 'unreachable',
                                       'detail': 'the host app did not answer'}
        self.convoy._reconcile()
        self.assertEqual(self.status_writes[-1], 'No Convoy host app')
        self.assertLen(self._warnings(), 1,
                       'unreachable can only follow a CONFIRMED live host, '
                       'so it is a real event -- but only one line')
        self.assertGreaterEqual(int(self.session.get('fails') or 0), 1)
        due = self.session.get('next_call_at')
        self.assertIsNotNone(due, 'a transport failure must schedule a retry')


class TestTickHygiene(ConvoyExtBase):
    """Storm collapse, poll generations, stale instances."""

    def setUp(self):
        super().setUp()
        self._reconciles = []
        self._patch(self.convoy, '_reconcile',
                    lambda *a, **kw: self._reconciles.append((a, kw)))
        self._saved_gen = self.comp.fetch('_convoy_gen', None, search=False)

    def tearDown(self):
        if self._saved_gen is None:
            try:
                self.comp.unstore('_convoy_gen')
            except Exception:
                pass
        else:
            self.comp.store('_convoy_gen', self._saved_gen)
        super().tearDown()

    def test_init_armed_a_positive_generation(self):
        gen = self.comp.fetch('_convoy_gen', 0)
        self.assertIsInstance(gen, int)
        self.assertGreater(gen, 0, 'a positive _convoy_gen proves the '
                                   'reconcile loop was armed from __init__')

    def test_stale_generation_collapses(self):
        self.comp.store('_convoy_gen', 42)
        self.convoy._convoyTick(41)
        self.assertEqual(self._reconciles, [],
                         'an older armed tick must not reconcile')
        self.assertEqual(self._tickReschedules(), [],
                         'and must not reschedule, or the storm never ends')

    def test_current_generation_runs_and_reschedules(self):
        self.comp.store('_convoy_gen', 42)
        self.convoy._convoyTick(42)
        self.assertLen(self._reconciles, 1)
        self.assertLen(self._tickReschedules(), 1)

    def test_legacy_zero_generation_is_never_orphaned(self):
        self.comp.store('_convoy_gen', 42)
        self.convoy._convoyTick(0)
        self.assertLen(self._reconciles, 1)
        self.assertLen(self._tickReschedules(), 1)

    def test_a_tick_error_never_kills_the_loop(self):
        def _boom(*a, **kw):
            raise RuntimeError('reconcile exploded')
        self._patch(self.convoy, '_reconcile', _boom)
        self.comp.store('_convoy_gen', 42)
        self.convoy._convoyTick(42)
        self.assertLen(self._tickReschedules(), 1,
                       'a transient tick error must never stop reconciling')

    def test_stale_instance_drops_its_poll(self):
        self._patch(self.convoy, '_staleInstance', lambda: True)
        self.convoy._busy = True
        self.convoy._result = {'_gen': 7, '_action': 'register',
                               'result': {'state': 'registered',
                                          'node_id': NODE_ID,
                                          'envoy_port': 9870}}
        self.convoy._pollCall('register', 7, 0)
        self.assertEqual(self.status_writes, [],
                         'a superseded instance must apply nothing')
        self.assertEqual(self._runs, [], 'and must not reschedule')
        # The stale exit CLEARS its instance's slot -- leaving it parked
        # orphaned the busy flag and wedged a Mac first-install session
        # (2026-08-04); same contract as the host/policy drains.
        self.assertIsNone(self.convoy._result,
                          'a stale poll clears its slot')
        self.assertFalse(self.convoy._busy,
                         'and releases the busy flag')

    def test_superseded_worker_generation_retries_instead_of_applying(self):
        self.convoy._result = {'_gen': 4, '_action': 'register',
                               'result': {'state': 'registered'}}
        self.convoy._pollCall('register', 5, 0)
        self.assertEqual(self.status_writes, [],
                         'an older generation must never be applied')
        self.assertLen(self._runs, 1, 'the poll re-arms instead')

    def test_poll_gives_up_with_an_honest_error(self):
        self.convoy._pollCall('register', 5, self.convoy.POLL_ATTEMPTS)
        self.assertStartsWith(self.status_writes[-1], 'Error:')
        self.assertFalse(self.convoy._busy)


class TestFirstEnableConfirmation(ConvoyExtBase):
    """A-13: ONE confirmation, the first time this INSTALL enables Convoy.

    The dialog is gated on an install-level marker, not the project, so these
    tests pin it False -- otherwise they would pass or fail depending on
    whether the developer running them had ever enabled Convoy on this
    machine. The skip path has its own test below.
    """

    def setUp(self):
        super().setUp()
        self._patch(self.convoy, '_installConsentGiven', lambda: False)
        self._patch(self.convoy, 'RecordInstallConsent', lambda: True)
        self.embody_target = self.embody.ext.Embody
        self.entry = {}
        self.dialogs = []
        self.recorded = []
        self.choice = 1
        self.candidate = 'cv_' + 'a' * 16

        def _messageBox(title, message, buttons):
            self.dialogs.append((title, message, list(buttons)))
            return self.choice

        def _ensureConvoyId(convoy_id=None, consent_scope=None,
                            binding_state=None):
            self.recorded.append((convoy_id, consent_scope, binding_state))
            return convoy_id or self.candidate

        self._patch(self.embody_target, '_readConvoyEntry',
                    lambda: dict(self.entry))
        self._patch(self.embody_target, '_mintConvoyId',
                    lambda: self.candidate)
        self._patch(self.embody_target, '_ensureConvoyId', _ensureConvoyId)
        self._patch(self.embody_target, '_messageBox', _messageBox)

    def test_consent_scope_is_the_lan_scope(self):
        self.assertEqual(self.convoy.CONSENT_SCOPE, CONSENT_SCOPE,
                         'the LAN build must never inherit the older '
                         'loopback-only grant')

    def test_a_second_project_on_a_consented_install_is_never_asked_again(self):
        """The trusted-LAN explanation is answered ONCE per install. A new
        project on the same machine mints its id silently -- re-asking turned
        a one-time explanation into a recurring modal. The Setup Wizard's
        Convoy step records the same marker, which is why enabling there is
        never followed by the dialog."""
        self._patch(self.convoy, '_installConsentGiven', lambda: True)
        assert self.convoy._ensureConsent() is True
        self.assertEqual(self.dialogs, [],
                         'an install that already consented must not be asked')
        self.assertLen(self.recorded, 1, 'the project id is still recorded')
        self.assertEqual(self.recorded[0][1], self.convoy.CONSENT_SCOPE)

    def test_confirm_mints_and_records_the_scope(self):
        self.choice = 1
        self.assertTrue(self.convoy._ensureConsent())
        self.assertLen(self.dialogs, 1)
        title, message, buttons = self.dialogs[0]
        self.assertNotIn(self.candidate, message,
                         'automatic membership should not expose an '
                         'implementation identifier in normal UX')
        self.assertIn('automatically join', message)
        self.assertIn(CONSENT_SCOPE, message,
                      'and the scope the user is granting')
        self.assertIn('project.json', message,
                      'and that the id lands in a COMMITTED file')
        self.assertEqual(buttons[0], 'Cancel',
                         'the safe answer is button 0')
        self.assertEqual(
            self.recorded,
            [(self.candidate, CONSENT_SCOPE, 'candidate')])
        self.assertEqual(self.id_writes[-1], self.candidate)
        self.assertEqual(self.enable_writes, [],
                         'a confirmed enable never flips the toggle')

    def test_cancel_flips_the_toggle_off_and_writes_nothing(self):
        self.choice = 0
        self.assertFalse(self.convoy._ensureConsent())
        self.assertEqual(self.recorded, [],
                         'nothing may be written on a decline')
        self.assertEqual(self.enable_writes, [False])
        self.assertEqual(self.status_writes[-1], 'Disabled')
        self.assertEqual(self._warnings(), [],
                         'declining a feature is not an error')

    def test_a_suppressed_dialog_is_a_decline(self):
        # -1 is what _messageBox returns during a save window or an
        # unseeded test. Any non-affirmative value means no.
        self.choice = -1
        self.assertFalse(self.convoy._ensureConsent())
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.enable_writes, [False])

    def test_an_already_recorded_convoy_never_re_prompts(self):
        self.entry = {'id': CONVOY_ID, 'consent_scope': CONSENT_SCOPE,
                      'granted_at': '2026-08-01T00:00:00Z'}
        self.assertTrue(self.convoy._ensureConsent())
        self.assertEqual(self.dialogs, [],
                         'consent is per PROJECT, and a clone inherits the '
                         'tracked key rather than re-asking')
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.id_writes[-1], CONVOY_ID)

    def test_a_legacy_loopback_grant_requires_explicit_upgrade(self):
        self.entry = {'id': CONVOY_ID,
                      'consent_scope': LEGACY_CONSENT_SCOPE,
                      'granted_at': '2026-08-01T00:00:00Z'}
        self.assertTrue(self.convoy._ensureConsent())
        self.assertLen(self.dialogs, 1)
        self.assertIn('Upgrade Convoy Access', self.dialogs[0][0])
        self.assertIn(LEGACY_CONSENT_SCOPE, self.dialogs[0][1])
        self.assertIn(CONSENT_SCOPE, self.dialogs[0][1])
        self.assertEqual(
            self.recorded,
            [(CONVOY_ID, CONSENT_SCOPE, 'established')])

    def test_background_reconcile_never_silently_widens_old_consent(self):
        self._patch(self.convoy, '_readConsentScope',
                    lambda: LEGACY_CONSENT_SCOPE)
        self.convoy._reconcile()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.enable_writes, [False])
        self.assertIn('Consent required', self.status_writes[-1])

    def test_an_unsaved_project_never_mints(self):
        """It must refuse AND SAY SO. Refusing silently is what happened to a
        macOS user on a fresh install: the wizard reported Convoy enabled, the
        toggle switched itself back off, and the only explanation was a
        textport line they had no reason to have open (2026-08-03)."""
        self._patch(self.convoy, '_savedToe', lambda: None)
        self.assertFalse(self.convoy._ensureConsent())
        self.assertEqual(self.recorded, [], 'nothing may be minted')
        self.assertEqual(self.enable_writes, [False])
        self.assertLen(self.dialogs, 1,
                       'an explicit enable that cannot proceed must tell the '
                       'user, not only the log')
        title, message, _buttons = self.dialogs[0]
        self.assertIn('Save', title)
        self.assertIn('never been saved', message)

    def test_a_failed_record_leaves_convoy_off(self):
        self._patch(self.embody_target, '_ensureConvoyId',
                    lambda convoy_id=None, consent_scope=None,
                    binding_state=None: '')
        self.choice = 1
        self.assertFalse(self.convoy._ensureConsent())
        self.assertEqual(self.enable_writes, [False])
        self.assertLen(self._warnings(), 1)

    def test_register_runs_the_gate_before_any_network_call(self):
        self.choice = 0
        self.convoy.Register()
        self.assertEqual(self.client.calls, [],
                         'a declined enable must not reach the host app')
        self.choice = 1
        self.convoy.Register()
        self.assertEqual(self.client.count('register'), 1)

    def test_explicit_enable_can_register_the_wake_plane_while_performing(self):
        self._patch(self.convoy, '_performing', lambda: True)
        self._patch(self.convoy, '_performRequested', lambda: True)
        self._patch(self.convoy, '_envoyPort', lambda: None)
        result = self.convoy.Register()
        self.assertNotEqual(result.get('state'), 'deferred')
        self.assertEqual(self.client.count('register'), 1)
        self.assertTrue(self.client.last('register')['perform_mode'])
