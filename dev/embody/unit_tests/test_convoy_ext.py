"""
Test suite: ConvoyExt, the node-side Convoy reconciler (Phase 2 Stage B).

NORMAL TIER. Nothing here touches ext.root, the live Convoy parameters, the
real host app, a socket, or a thread. Three seams make that true:

  - ``_client`` is patched to a StubClient that WRAPS the real
    convoy_client module and replaces only its three network functions
    (probe / register / unregister). Every pure helper the extension
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
    TDNExt's SKIP_STORAGE_KEYS, EnvoyExt.RuntimePort() exists.
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

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

CONSENT_SCOPE = 'local host app only'
NODE_ID = 'n' * 32
HOST_ID = 'h' * 32
OTHER_HOST_ID = 'x' * 32
CONVOY_ID = 'cv_' + '0' * 16


class StubClient:
    """The REAL convoy_client with its three network seams replaced.

    Attribute lookups fall through to the live module, so every constant
    and every pure helper under test is the shipping one. Only probe,
    register and unregister are stubs -- which is exactly the set that
    would otherwise open a socket.
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

    def __getattr__(self, name):
        return getattr(self._real, name)

    # -- the three replaced seams -------------------------------------
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
                                          'runtime_id': runtime_id}))
        return dict(self.unregister_result)

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
                     '_pollCall'):
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
        """Both readouts must be scrubbed on export, and each must use
        the registry mechanism that FITS it.

        Convoystatus is a state, so it names a truthy resting string.
        Convoyid is an IDENTIFIER that legitimately rests empty (no
        convoy until the first explicit enable), so it registers None --
        which the scrub reads as 'reset to the par's own default'. A
        literal '' resting is banned outright by the state-machine
        invariant in test_release_hooks, and would fail there. What
        matters for A-50 is only that the name IS registered: unregistered,
        this machine's convoy id bakes into the tracked Embody.tdn and
        into every released .tox.
        """
        registry = self.embody_ext._TRANSIENT_STATUS_PARS['Embody']
        self.assertEqual(registry.get('Convoystatus'), 'Disabled')
        self.assertIn(
            'Convoyid', registry,
            "Convoyid MUST be registered -- unregistered, this machine's "
            'convoy id bakes into the tracked Embody.tdn and every '
            'released .tox (A-50)')
        self.assertIsNone(
            registry['Convoyid'],
            'Convoyid rests at its default (empty), which the registry '
            'spells None; a literal "" is refused by the resting-value '
            'invariant')
        # And the default it resets TO must actually be the empty state.
        self.assertEqual(self.embody.par.Convoyid.default, '')

        # Convoyhoststatus is the third readout, and it leaks harder than
        # either: it names a pid, a version and a supervisor that exist
        # only on the machine that installed them. Unregistered, one
        # developer's 'Running 6.0.173 (pid 24180)' bakes into the tracked
        # Embody.tdn and tells every downloader their host app is already
        # running when nothing is installed. It IS a state, so unlike
        # Convoyid it names a truthy resting.
        self.assertEqual(
            registry.get('Convoyhoststatus'), 'Not installed',
            'Convoyhoststatus must rest at the honest answer for a '
            'machine that has not installed the host app (A-50)')
        self.assertEqual(
            self.embody.par.Convoyhoststatus.default, 'Not installed',
            'the resting and the default must agree, or a scrub and a '
            'revert-to-default disagree about the same par')
        self.assertTrue(
            self.embody.par.Convoyhoststatus.readOnly,
            'Convoyhoststatus is a readout, not an input')

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
        skip = self.embody.op('TDNExt').module.SKIP_STORAGE_KEYS
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
        self.session = {}

        self._patch(self.convoy, '_client', lambda: self.client)
        self._patch(self.convoy, '_runInWorker', lambda fn: fn())
        self._patch(self.convoy_mod, 'run', self._fakeRun)
        self._patch(self.convoy, '_log',
                    lambda msg, level='INFO': self._logs.append((msg, level)))
        self._patch(self.convoy, '_status',
                    lambda text: self.status_writes.append(str(text)))
        self._patch(self.convoy, '_publishId',
                    lambda value: self.id_writes.append(str(value or '')))
        self._patch(self.convoy, '_setEnabled',
                    lambda value: self.enable_writes.append(bool(value)))
        self._patch(self.convoy, '_session', lambda: self.session)
        self._patch(self.convoy, '_enabled', lambda: True)
        self._patch(self.convoy, '_performing', lambda: False)
        self._patch(self.convoy, '_savedToe', lambda: 'C:/fake/project.toe')
        self._patch(self.convoy, '_readConvoyId', lambda: CONVOY_ID)
        self._patch(self.convoy, '_envoyPort', lambda: 9870)
        # Instance bookkeeping the reconciler mutates.
        self._patch(self.convoy, '_busy', False)
        self._patch(self.convoy, '_result', None)
        self._patch(self.convoy, '_logged', '')
        self._patch(self.convoy, '_gen', 0)
        self._patch(self.convoy, '_tick_ms', self.convoy.TICK_MIN_MS)

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

    # -- helpers ------------------------------------------------------
    def _warnings(self):
        return [m for m, level in self._logs if level == 'WARNING']

    def _tickReschedules(self):
        return [a for a, _kw in self._runs
                if a and isinstance(a[0], str) and '_convoyTick' in a[0]]


class TestReconcileCalls(ConvoyExtBase):
    """The whole point of D2: one mechanism, no redundant network calls."""

    def test_first_pass_registers_once(self):
        self.convoy._reconcile()
        self.assertEqual(self.client.count('register'), 1)
        payload = self.client.last('register')
        self.assertEqual(payload['convoy_id'], CONVOY_ID)
        self.assertEqual(payload['comp_path'], self.embody.path)
        self.assertEqual(payload['envoy_port'], 9870)
        self.assertTrue(payload['runtime_id'].startswith('rt_'),
                        'runtime_id is REQUIRED on every call: the host '
                        're-mints the run identity when it is omitted')
        self.assertTrue(self.session.get('registered'))
        self.assertEqual(self.session.get('node_id'), NODE_ID)

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
        for key in ('enabled', 'performing', 'saved_project', 'convoy_id',
                    'node_id', 'host_id', 'runtime_id', 'registered',
                    'envoy_port', 'busy', 'status'):
            self.assertDictHasKey(snap, key)
        self.assertNotIn('error', snap, 'the snapshot must never raise')
        self.assertEqual(snap['node_id'], NODE_ID)
        self.assertEqual(snap['convoy_id'], CONVOY_ID)
        self.assertTrue(snap['registered'])

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


class TestQuietRefusals(ConvoyExtBase):
    """Absence, Perform Mode and an unsaved project are not failures."""

    def test_perform_mode_does_zero_work_and_never_writes_status(self):
        self._patch(self.convoy, '_performing', lambda: True)
        self.convoy._reconcile()
        self.assertEqual(self.client.calls, [])
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
        self.convoy._result = {'_gen': 7, '_action': 'register',
                               'result': {'state': 'registered',
                                          'node_id': NODE_ID,
                                          'envoy_port': 9870}}
        self.convoy._pollCall('register', 7, 0)
        self.assertEqual(self.status_writes, [],
                         'a superseded instance must apply nothing')
        self.assertEqual(self._runs, [], 'and must not reschedule')
        self.assertIsNotNone(self.convoy._result,
                             'and must not consume the slot')

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
    """A-13: one confirmation, per project, on the first EXPLICIT enable."""

    def setUp(self):
        super().setUp()
        self.embody_target = self.embody.ext.Embody
        self.entry = {}
        self.dialogs = []
        self.recorded = []
        self.choice = 1
        self.candidate = 'cv_' + 'a' * 16

        def _messageBox(title, message, buttons):
            self.dialogs.append((title, message, list(buttons)))
            return self.choice

        def _ensureConvoyId(convoy_id=None, consent_scope=None):
            self.recorded.append((convoy_id, consent_scope))
            return convoy_id or self.candidate

        self._patch(self.embody_target, '_readConvoyEntry',
                    lambda: dict(self.entry))
        self._patch(self.embody_target, '_mintConvoyId',
                    lambda: self.candidate)
        self._patch(self.embody_target, '_ensureConvoyId', _ensureConvoyId)
        self._patch(self.embody_target, '_messageBox', _messageBox)

    def test_consent_scope_is_the_phase2_scope(self):
        self.assertEqual(self.convoy.CONSENT_SCOPE, CONSENT_SCOPE,
                         'Phase 3 must be able to detect a widening')

    def test_confirm_mints_and_records_the_scope(self):
        self.choice = 1
        self.assertTrue(self.convoy._ensureConsent())
        self.assertLen(self.dialogs, 1)
        title, message, buttons = self.dialogs[0]
        self.assertIn(self.candidate, message,
                      'the dialog must NAME the id it is about to mint')
        self.assertIn(CONSENT_SCOPE, message,
                      'and the scope the user is granting')
        self.assertIn('project.json', message,
                      'and that the id lands in a COMMITTED file')
        self.assertEqual(buttons[0], 'Cancel',
                         'the safe answer is button 0')
        self.assertEqual(self.recorded, [(self.candidate, CONSENT_SCOPE)])
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

    def test_an_unsaved_project_never_mints(self):
        self._patch(self.convoy, '_savedToe', lambda: None)
        self.assertFalse(self.convoy._ensureConsent())
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.enable_writes, [False])

    def test_a_failed_record_leaves_convoy_off(self):
        self._patch(self.embody_target, '_ensureConvoyId',
                    lambda convoy_id=None, consent_scope=None: '')
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

    def test_register_is_a_no_op_while_performing(self):
        self._patch(self.convoy, '_performing', lambda: True)
        result = self.convoy.Register()
        self.assertEqual(result.get('state'), 'deferred')
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.client.calls, [])
