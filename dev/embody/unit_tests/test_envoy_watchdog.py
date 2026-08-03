"""
Test suite: Envoy save-resilient liveness watchdog (v6.0.11).

The watchdog is a pure run()-loop, armed once per EnvoyExt instance from
__init__ (NOT from Start). It probes the real MCP socket every ~4s and revives
Envoy whenever it is enabled-but-down -- a dropped-socket zombie, a never-bound
restart, or a suppressed reinit Start -- so every connected bridge reconnects on
its own with no manual toggle.

These tests are state/mocked: they NEVER start (or kill) a real server. They
drive the watchdog methods directly with the module-level run() scheduler patched
out (so no Start() is ever actually dispatched) and self._log patched out (so the
real logger is never touched, and WARNING emissions can be counted). Every piece
of mutated state -- _last_revive_frame, envoy_running, _server_gen, Envoystatus,
_deadTicks, _starting, _startingTicks, _runtime_port -- is snapshotted in setUp
and restored in tearDown, so the live MCP server is left exactly as found.

Coverage:
  - The watchdog methods exist, and __init__ armed _watchdog_gen > 0 on the COMP.
  - LOG-STORM COOLDOWN (headline): many same-instant _reviveDeadServer() calls
    collapse to exactly ONE revive side-effect; a later death (past the 2s
    time.monotonic() cooldown) revives again. REGRESSION: a stale-HIGH persisted
    _last_revive_frame must NOT block a revive -- the cross-session wedge where a
    saved absTime.frame from a prior session went negative against this session's
    counter and permanently no-op'd recovery.
  - Stale-generation tick (gen < COMP gen) -> no reschedule / revive / log;
    current gen -> reschedules.
  - Revive-when-down: enabled + init-complete + not-starting + dead socket for
    >=2 ticks -> revive; disabled / pre-init branches reset _deadTicks and never
    revive.
  - _probeAlive against a real stdlib socket listener: open -> True, closed ->
    False, None port -> True.
  - _findAvailablePort fast path + force-free branch with monkeypatched helpers;
    bind-probe vs zombie-held ports (bound but dead -- connects refused, bind
    blocked) and the recent-bind-failure blacklist (record on pre-bind death,
    skip while fresh, expire after TTL, clear on confirmed bind).
  - 'Preparing Python environment...' (the fast-path import gate) is
    TRANSITIONAL: no dead-socket revive mid-warmup; a wedged gate still
    self-heals via the ~24s startup-grace restart.
  - _run_tests Status stomp: the prior Status survives in COMP storage and
    _restoreStatusAfterTests is idempotent, never resurrecting 'Testing'.
  - PERFORM MODE (v6.0.169): Perform Mode is an idle condition -- no probe, no
    dead-tick accrual, no revive, 'Perform Mode' status never clobbered --
    gated on the live par authority (_performModeActive), never the status
    string. Start() refuses while performing (queued revive/restart Starts
    armed before Perform entry die here); the Perform-exit restart passes the
    gate; the watchdog resumes normal revive duty after exit. The Perform
    signal is STUBBED (instance patch) -- toggling the live Performmode par
    would sever MCP with no command path back.
  - PERFORM AUTHORITY + POLL GATES (v6.0.169, panel findings): the REAL
    _performModeActive wire is exercised unstubbed (matches
    EmbodyExt._performMode; exceptions read False so self-healing is never
    disabled), and the three startup polls (_pollImportGate, _pollBootstrap,
    _pollStartup) honor Perform -- an in-flight start crossing a Perform
    entry aborts or tears down instead of completing mid-show, with the
    Perform-off passthrough pinned.
"""

import socket
import sys
import threading
import time

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class EnvoyWatchdogBase(EmbodyTestCase):
    """Shared fixture for watchdog contracts. NEVER starts a real server.

    The module-level run() scheduler is patched to RECORD scheduled calls
    instead of dispatching them, so a revive's "schedule Start()" side effect
    is observable without a server ever starting. _log is patched to record
    (level, message) tuples so WARNING emissions can be counted without
    touching the centralized logger.
    """

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.envoy_mod = self.embody.op('EnvoyExt').module
        self._patches = []
        self._runs = []           # recorded run() scheduler calls (a, kw)
        self._logs = []           # recorded _log() calls (message, level)

        # Snapshot every piece of state these tests mutate so the live server
        # is left exactly as found. COMP-stored values survive reinit; instance
        # attributes do not -- snapshot both kinds.
        comp = self.embody
        self._saved_store = {
            'envoy_running': comp.fetch('envoy_running', None, search=False),
            '_last_revive_frame': comp.fetch('_last_revive_frame', None, search=False),
            '_watchdog_gen': comp.fetch('_watchdog_gen', None, search=False),
            '_init_complete': comp.fetch('_init_complete', None, search=False),
        }
        self._saved_attr = {
            '_server_gen': self.envoy._server_gen,
            '_deadTicks': getattr(self.envoy, '_deadTicks', 0),
            '_startingTicks': getattr(self.envoy, '_startingTicks', 0),
            '_starting': getattr(self.envoy, '_starting', False),
            '_runtime_port': getattr(self.envoy, '_runtime_port', None),
            '_last_revive_time': getattr(self.envoy, '_last_revive_time', 0.0),
        }
        self._saved_enable = comp.par.Envoyenable.eval()
        self._saved_status = comp.par.Envoystatus.eval()

        # Patch the module-level run() so NO Start()/tick is ever dispatched.
        # Both the tick reschedule and _reviveDeadServer's Start() go through
        # this; recording them lets us assert side effects without a real start.
        self._patch(self.envoy_mod, 'run',
                    lambda *a, **kw: self._runs.append((a, kw)))
        # Patch the instance _log so WARNINGs are counted, not logged for real.
        self._patch(self.envoy, '_log',
                    lambda message, level='INFO': self._logs.append((message, level)))
        # Tests must NEVER signal the LIVE server's shutdown event: a real
        # _reviveDeadServer() call does self.shutdown_event.set(), which
        # bounced the actual MCP server mid-full-run and poisoned later
        # suites (2026-07-16 full-run-only failures in shortcuts/rename/
        # watchdog). Swap in a throwaway Event; the patch-restore in
        # tearDown puts the live one back untouched.
        import threading as _threading
        self._patch(self.envoy, 'shutdown_event', _threading.Event())

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

        comp = self.embody
        for key, val in self._saved_store.items():
            if val is None:
                try:
                    comp.unstore(key)
                except Exception:
                    pass
            else:
                comp.store(key, val)
        for name, val in self._saved_attr.items():
            setattr(self.envoy, name, val)
        comp.par.Envoystatus = self._saved_status
        comp.par.Envoyenable = self._saved_enable

        super().tearDown()

    def _patch(self, obj, name, value):
        sentinel = object()
        old = getattr(obj, name, sentinel)
        setattr(obj, name, value)
        self._patches.append((obj, name, old, sentinel))

    def _warning_count(self):
        return sum(1 for _msg, level in self._logs if level == 'WARNING')

    def _start_schedule_count(self):
        """Count recorded run() calls that schedule Envoy.Start()."""
        n = 0
        for a, _kw in self._runs:
            if a and isinstance(a[0], str) and '.Start()' in a[0]:
                n += 1
        return n


class TestWatchdogArming(EnvoyWatchdogBase):
    """The watchdog methods exist and __init__ armed a positive generation."""

    def test_watchdog_methods_exist(self):
        for name in ('_watchdogTick', '_probeAlive', '_reviveDeadServer',
                     '_findAvailablePort'):
            self.assertTrue(
                callable(getattr(self.envoy, name, None)),
                f'EnvoyExt must expose a callable {name}')

    def test_init_armed_watchdog_generation(self):
        # __init__ does fetch('_watchdog_gen', 0) + 1, store(...). Whatever the
        # live count, it must be a positive int -- proof the loop was armed.
        gen = self.embody.fetch('_watchdog_gen', 0)
        self.assertIsInstance(gen, int)
        self.assertGreater(gen, 0,
                           'A positive _watchdog_gen proves the watchdog was armed')


class TestReviveLogStormCooldown(EnvoyWatchdogBase):
    """HEADLINE: many same-frame revives collapse to ONE; a later death revives."""

    def test_same_frame_revives_collapse_to_one(self):
        # Reset the cooldown so the FIRST call is allowed to fire. The 5 calls run
        # within the same ~microsecond window, so every call after the first sits
        # inside the 2s time.monotonic() cooldown and is dropped.
        self.envoy._last_revive_time = 0.0
        self.envoy._deadTicks = 0

        for _ in range(5):
            self.envoy._reviveDeadServer(was_running=True)

        # Exactly one revive: one Start() scheduled, one WARNING logged.
        self.assertEqual(
            self._start_schedule_count(), 1,
            'A same-frame revive storm must schedule Start() exactly once')
        self.assertEqual(
            self._warning_count(), 1,
            'A same-frame revive storm must log exactly one WARNING')
        # And the status was flipped by the single revive that went through.
        self.assertStartsWith(str(self.embody.par.Envoystatus.eval()), 'Reviving')

    def test_later_death_past_cooldown_revives_again(self):
        # First death -> one revive.
        self.envoy._last_revive_time = 0.0
        self.envoy._reviveDeadServer(was_running=True)
        self.assertEqual(self._start_schedule_count(), 1)
        self.assertEqual(self._warning_count(), 1)

        # Simulate a genuinely later outage by backdating the recorded revive
        # time well beyond the 2s monotonic cooldown.
        self.envoy._last_revive_time = time.monotonic() - 3.0

        self.envoy._reviveDeadServer(was_running=False)
        self.assertEqual(
            self._start_schedule_count(), 2,
            'A death past the cooldown must schedule a second Start()')
        self.assertEqual(
            self._warning_count(), 2,
            'A death past the cooldown must log a second WARNING')

    def test_revive_bumps_server_generation_and_clears_running(self):
        self.envoy._last_revive_time = 0.0
        self.embody.store('envoy_running', True)
        # Model a STUCK start explicitly: _starting still set but its
        # startup window long expired. Revive must proceed and clear it.
        # (A start INSIDE its window is deliberately not revivable -- the
        # in-flight guard added for the 2026-07-15 restart storm -- and a
        # full test run can leave a live future deadline behind, so pin it.)
        self.envoy._starting = True
        self.envoy._startup_deadline = 0.0
        gen_before = self.envoy._server_gen

        self.envoy._reviveDeadServer(was_running=True)

        self.assertEqual(
            self.envoy._server_gen, gen_before + 1,
            'Revive must bump _server_gen so stale callbacks are ignored')
        self.assertFalse(
            self.embody.fetch('envoy_running', True, search=False),
            'Revive must clear envoy_running')
        self.assertFalse(
            self.envoy._starting,
            'Revive must clear _starting')

    def test_stale_persisted_frame_does_not_block_revive(self):
        """REGRESSION (the cross-session wedge): a high _last_revive_frame left in
        COMP storage by a prior session -- larger than this session's absTime.frame
        -- must NOT block the revive. The cooldown is now time.monotonic() on an
        instance attribute and never reads the persisted frame. Pre-fix, the
        negative (now - stored) delta sat permanently inside the cooldown window
        and silently no-op'd every revive: detection fired forever, restart never."""
        # Poison: a persisted frame far above any plausible session absTime.frame.
        self.embody.store('_last_revive_frame', 999_999_999)
        self.envoy._last_revive_time = 0.0          # cooldown genuinely expired
        self.envoy._deadTicks = 0

        self.envoy._reviveDeadServer(was_running=True)

        self.assertEqual(
            self._start_schedule_count(), 1,
            'A revive must fire despite a stale-high persisted _last_revive_frame')
        self.assertEqual(
            self._warning_count(), 1,
            'A revive must log its WARNING despite the stale persisted frame')
        self.assertStartsWith(
            str(self.embody.par.Envoystatus.eval()), 'Reviving',
            'The revive must flip status to Reviving, not silently no-op')


class TestWatchdogTickGeneration(EnvoyWatchdogBase):
    """Stale-generation ticks exit silently; the current generation reschedules."""

    def test_stale_generation_tick_is_inert(self):
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        stale_gen = live_gen + 1  # gen != COMP gen and gen is truthy -> stale

        runs_before = len(self._runs)
        logs_before = len(self._logs)
        deadticks_before = self.envoy._deadTicks

        self.envoy._watchdogTick(stale_gen)

        self.assertEqual(
            len(self._runs), runs_before,
            'A stale-generation tick must NOT reschedule')
        self.assertEqual(
            len(self._logs), logs_before,
            'A stale-generation tick must NOT log')
        self.assertEqual(
            self.envoy._deadTicks, deadticks_before,
            'A stale-generation tick must NOT touch _deadTicks')
        self.assertEqual(
            self._start_schedule_count(), 0,
            'A stale-generation tick must NOT revive')

    def test_current_generation_tick_reschedules(self):
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        # Keep the branch inert (disabled) so the only side effect is the
        # always-on reschedule at the end of the tick.
        self.embody.par.Envoyenable = 0

        runs_before = len(self._runs)
        self.envoy._watchdogTick(live_gen)

        # Exactly one new run() -- the tick's own reschedule.
        self.assertEqual(
            len(self._runs), runs_before + 1,
            'The current-generation tick must reschedule itself exactly once')
        a, _kw = self._runs[-1]
        self.assertTrue(
            a and isinstance(a[0], str) and '._watchdogTick(' in a[0],
            'The reschedule must call _watchdogTick again')

    def test_legacy_gen_zero_tick_proceeds(self):
        # gen == 0 is a legacy tick armed before the generation guard existed;
        # it must NOT be treated as stale -- it proceeds and reschedules.
        self.embody.par.Envoyenable = 0
        runs_before = len(self._runs)

        self.envoy._watchdogTick(0)

        self.assertEqual(
            len(self._runs), runs_before + 1,
            'A legacy gen==0 tick must proceed and reschedule (never orphaned)')


class TestWatchdogReviveWhenDown(EnvoyWatchdogBase):
    """Enabled-but-down for >=2 ticks revives; disabled/pre-init never revives."""

    def setUp(self):
        super().setUp()
        # Record revive calls instead of running the real revive (which would
        # bump generation / flip status / schedule Start). The tick-level branch
        # logic is what we are testing here, not the revive body.
        self._revives = []
        self._patch(self.envoy, '_reviveDeadServer',
                    lambda was_running: self._revives.append(was_running))

    def test_dead_socket_two_ticks_triggers_revive(self):
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.store('_init_complete', True)
        self.embody.store('envoy_running', True)
        self.envoy._starting = False
        self.envoy._deadTicks = 1            # one short of the >=2 threshold
        self._patch(self.envoy, '_probeAlive', lambda: False)

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 1,
            'A dead socket reaching the 2-tick threshold must revive')
        self.assertEqual(
            self.envoy._deadTicks, 0,
            '_deadTicks must reset to 0 after a revive fires')

    def test_dead_socket_one_tick_does_not_revive(self):
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.store('_init_complete', True)
        self.embody.store('envoy_running', True)
        self.envoy._starting = False
        self.envoy._deadTicks = 0            # first dead tick: increments to 1
        self._patch(self.envoy, '_probeAlive', lambda: False)

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 0,
            'A single dead tick must NOT revive (threshold is >=2)')
        self.assertEqual(
            self.envoy._deadTicks, 1,
            'The first dead tick must increment _deadTicks to 1')

    def test_live_socket_resets_dead_ticks(self):
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.store('_init_complete', True)
        self.embody.store('envoy_running', True)
        self.envoy._starting = False
        self.envoy._deadTicks = 1
        self._patch(self.envoy, '_probeAlive', lambda: True)

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 0,
            'A live socket must never revive')
        self.assertEqual(
            self.envoy._deadTicks, 0,
            'A live socket must reset _deadTicks to 0')

    def test_disabled_branch_resets_and_never_revives(self):
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 0     # disabled -> idle branch
        self.embody.store('_init_complete', True)
        self.envoy._deadTicks = 5
        self.envoy._startingTicks = 3
        # Probe must NOT even be consulted on the disabled branch.
        self._patch(self.envoy, '_probeAlive',
                    lambda: (_ for _ in ()).throw(
                        AssertionError('probe must not run while disabled')))

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 0,
            'The disabled branch must never revive')
        self.assertEqual(self.envoy._deadTicks, 0,
                         'The disabled branch must reset _deadTicks')
        self.assertEqual(self.envoy._startingTicks, 0,
                         'The disabled branch must reset _startingTicks')

    def test_installing_status_idles_and_resets(self):
        """A one-time deps install ('Installing deps...') must idle the watchdog:
        no probe, no revive, counters reset. This is the grace state that REPLACES
        the old _init_complete pre-init branch -- the socket-truth watchdog keys
        off the visible status, and 'Installing' is the legitimately-long op it
        must never interrupt."""
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Installing deps... (one-time)'
        self.envoy._deadTicks = 4
        self.envoy._startingTicks = 2
        self._patch(self.envoy, '_probeAlive',
                    lambda: (_ for _ in ()).throw(
                        AssertionError('probe must not run while installing')))

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 0,
            'The installing branch must never revive')
        self.assertEqual(self.envoy._deadTicks, 0,
                         'The installing branch must reset _deadTicks')
        self.assertEqual(self.envoy._startingTicks, 0,
                         'The installing branch must reset _startingTicks')

    def test_init_complete_false_no_longer_blocks_revive(self):
        """SOCKET-TRUTH (the save-wedge fix): _init_complete=False must NOT idle
        the watchdog when the status is settled and the socket is dead. A save
        unstores _init_complete, and the old `enabled and init_done` gate sent the
        tick idle -- wedging a dead server forever. Now the socket is the truth: a
        dead socket while enabled revives regardless of _init_complete."""
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.store('_init_complete', False)            # the save-cleared state
        self.embody.par.Envoystatus = 'Running on port 9870'  # settled, stale
        self.embody.store('envoy_running', True)
        self.envoy._starting = False
        self.envoy._deadTicks = 1                              # one short of >=2
        self._patch(self.envoy, '_probeAlive', lambda: False)  # socket dead

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 1,
            'A dead socket while enabled must revive even with _init_complete=False')

    def test_preparing_status_is_transitional_not_dead(self):
        """REGRESSION (issue #60 follow-up): 'Preparing Python environment...'
        is the fast-path import gate -- a healthy in-flight startup with no
        socket bound yet. The watchdog used to classify it as settled: probe
        dead for 2 ticks -> revive at ~8s, stomping a cold first open that
        legitimately takes longer (observed: revive 7s after launch while the
        gate was still importing). It must take the transitional branch -- no
        probe, no dead-tick revive, startup grace accrues instead."""
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Preparing Python environment...'
        self.embody.store('envoy_running', False)
        self.envoy._starting = False
        self.envoy._deadTicks = 1            # would revive on the next dead tick
        self.envoy._startingTicks = 0
        self._patch(self.envoy, '_probeAlive',
                    lambda: (_ for _ in ()).throw(
                        AssertionError('probe must not run while preparing')))

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 0,
            "'Preparing...' must never take the dead-socket revive path")
        self.assertEqual(
            self.envoy._deadTicks, 0,
            "'Preparing...' must reset _deadTicks (transitional branch)")
        self.assertEqual(
            self.envoy._startingTicks, 1,
            "'Preparing...' must accrue startup grace like the other "
            "transitional states")

    def test_preparing_stuck_24s_forces_restart(self):
        """A genuinely wedged 'Preparing...' (an orphaned import gate after a
        mid-warmup reinit, whose stale-instance poll exits without finishing
        the start) must still self-heal via the ~24s startup-grace restart."""
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Preparing Python environment...'
        self.envoy._deadTicks = 0
        self.envoy._startingTicks = 5        # one short of the >=6 threshold

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 1,
            "A 'Preparing...' stuck past ~24s must force a restart")
        self.assertEqual(
            self.envoy._startingTicks, 0,
            'The forced restart must reset _startingTicks')


class TestWatchdogPerformMode(EnvoyWatchdogBase):
    """Perform Mode idles the watchdog and refuses Start() (v6.0.169 fix).

    _enterPerformMode Stop()s the server but deliberately leaves Envoyenable
    True (config.json integrity), so enabled-but-dead is the EXPECTED state
    during a show. Pre-fix, the watchdog classified 'Perform Mode' as settled,
    probed the dead socket, revived Envoy ~4-12s after Perform entry, and
    clobbered the status readout with 'Reviving (watchdog)...'. The thread-exit
    hooks (_onServerSuccess/_onServerError) already guarded on _performMode;
    these tests pin the watchdog + Start() to the same authority.

    The Perform signal is stubbed by patching the instance's
    _performModeActive -- NEVER by toggling the live Performmode par: with the
    fix in place a real Perform entry severs MCP with no command path back,
    and the standing rule forbids recovering by killing/restarting TD.
    """

    def setUp(self):
        super().setUp()
        # Record revive calls instead of running the real revive body.
        self._revives = []
        self._patch(self.envoy, '_reviveDeadServer',
                    lambda was_running: self._revives.append(was_running))
        # Sandbox-scoped Perform stub, flippable mid-test for the exit cases.
        self._perform = {'on': True}
        self._patch(self.envoy, '_performModeActive',
                    lambda: self._perform['on'])

    def test_perform_on_dead_socket_idles_no_accrual_no_revive(self):
        """Perform on + server stopped: no probe, no dead-tick accrual, no
        revive -- the enabled-but-dead state is expected, not an outage."""
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1          # Perform leaves this True
        self.embody.par.Envoystatus = 'Perform Mode'
        self.embody.store('envoy_running', False)  # Stop() cleared it
        self.envoy._starting = False
        self.envoy._deadTicks = 1                # one short of the >=2 threshold
        self.envoy._startingTicks = 3
        self._patch(self.envoy, '_probeAlive',
                    lambda: (_ for _ in ()).throw(
                        AssertionError('probe must not run during Perform Mode')))

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 0,
            'Perform Mode must never take the dead-socket revive path')
        self.assertEqual(
            self.envoy._deadTicks, 0,
            'Perform Mode must reset _deadTicks, never accrue them')
        self.assertEqual(
            self.envoy._startingTicks, 0,
            'Perform Mode must reset _startingTicks')

    def test_perform_on_status_readout_not_clobbered(self):
        """Repeated ticks during Perform must leave the 'Perform Mode' status
        readout untouched and schedule no Start() -- pre-fix the second tick
        overwrote it with 'Reviving (watchdog)...'. The REAL _reviveDeadServer
        is exposed for this test (the class stub is removed) so the status
        assertion has teeth: were the Perform gate absent, the real revive
        body would flip the status (panel finding -- with the stub in place
        this assertion could never fail)."""
        delattr(self.envoy, '_reviveDeadServer')   # expose the real body
        self.envoy._last_revive_time = 0.0         # cooldown must not mask it
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Perform Mode'
        self.embody.store('envoy_running', False)
        self.envoy._starting = False
        self.envoy._deadTicks = 0

        for _ in range(3):                       # > the pre-fix 2-tick fuse
            self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            str(self.embody.par.Envoystatus.eval()), 'Perform Mode',
            "The 'Perform Mode' status readout must survive watchdog ticks")
        self.assertEqual(
            self._start_schedule_count(), 0,
            'No Start() may be scheduled while Perform Mode is active')
        self.assertEqual(
            self.envoy._deadTicks, 0,
            'Perform ticks must keep resetting _deadTicks (idle branch)')

    def test_perform_off_dead_socket_still_revives(self):
        """REGRESSION: the shipped enabled-but-down revive (a save-wedge /
        zombie-socket incident fix) must keep working when Perform is off."""
        self._perform['on'] = False
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Running on port 9872'
        self.embody.store('envoy_running', True)
        self.envoy._starting = False
        self.envoy._deadTicks = 1                # one short of the >=2 threshold
        self._patch(self.envoy, '_probeAlive', lambda: False)

        self.envoy._watchdogTick(live_gen)

        self.assertEqual(
            len(self._revives), 1,
            'With Perform off, a dead socket reaching the 2-tick threshold '
            'must still revive -- the Perform gate must not break self-healing')

    def test_start_refused_during_perform(self):
        """A queued Start() (revive/restart armed before Perform entry) landing
        mid-show must refuse with a clear log and mutate nothing."""
        self._perform['on'] = True
        self.embody.par.Envoyenable = 1          # master switch alone must not admit it
        starting_before = self.envoy._starting

        self.envoy.Start()

        self.assertTrue(
            any('Perform Mode' in msg and level == 'WARNING'
                for msg, level in self._logs),
            'The Perform-Mode refusal must log a clear WARNING')
        self.assertEqual(
            self._start_schedule_count(), 0,
            'A refused Start() must not schedule anything')
        self.assertEqual(
            self.envoy._starting, starting_before,
            'A refused Start() must not flip _starting')

    def test_perform_exit_start_passes_gate(self):
        """The Perform-exit restart runs AFTER the live Performmode par is off,
        so Start() must pass the Perform gate then. envoy_running is pinned
        True so the safe duplicate-start check exits Start() right after the
        gate -- proving passthrough without ever spawning a real server."""
        self._perform['on'] = False
        self.embody.par.Envoyenable = 1
        self.embody.store('envoy_running', True)
        self.envoy._starting = False

        self.envoy.Start()

        self.assertFalse(
            any('Start refused -- Perform Mode' in msg for msg, _lvl in self._logs),
            'With Perform off, Start() must not trip the Perform refusal')
        self.assertTrue(
            any('duplicate Start ignored' in msg for msg, _lvl in self._logs),
            'Start() must have reached the duplicate-start check -- proof it '
            'passed the Perform gate and took the normal path')

    def test_watchdog_resumes_duty_after_perform_exit(self):
        """A tick during Perform idles; once Perform ends, the same loop must
        resume normal revive duty (dead-tick accrual and the 2-tick revive)."""
        live_gen = self.embody.fetch('_watchdog_gen', 1)
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Perform Mode'
        self.embody.store('envoy_running', True)
        self.envoy._starting = False
        self.envoy._deadTicks = 0
        self._patch(self.envoy, '_probeAlive', lambda: False)

        self.envoy._watchdogTick(live_gen)       # performing -> idle
        self.assertEqual(len(self._revives), 0)
        self.assertEqual(self.envoy._deadTicks, 0)

        # Perform exits; the status settles back to a running readout.
        self._perform['on'] = False
        self.embody.par.Envoystatus = 'Running on port 9872'

        self.envoy._watchdogTick(live_gen)       # dead tick 1 -- below threshold
        self.assertEqual(
            len(self._revives), 0,
            'The first post-Perform dead tick must not revive yet (threshold 2)')
        self.assertEqual(
            self.envoy._deadTicks, 1,
            'Post-Perform ticks must accrue _deadTicks again')

        self.envoy._watchdogTick(live_gen)       # dead tick 2 -- revive
        self.assertEqual(
            len(self._revives), 1,
            'The watchdog must resume normal revive duty after Perform exit')


class TestPerformModeAuthority(EnvoyWatchdogBase):
    """The REAL _performModeActive() wire (no stubs in this class).

    Every TestWatchdogPerformMode test stubs the helper, so a silent break in
    the EnvoyExt -> EmbodyExt._performMode chain (a property rename, a broken
    ext ref) would return False forever -- reintroducing the shipped bug while
    the whole suite stays green (panel finding). These pin the two contracts:
    the helper reads the narrow Envoy authority, and errors read as False
    (self-healing is never disabled by a broken reference). Read-only: the
    live Performmode par is never toggled.
    """

    def test_helper_matches_live_authority(self):
        self.assertEqual(
            self.envoy._performModeActive(),
            bool(self.embody.ext.Embody._envoyPerformMode),
            '_performModeActive must read EmbodyExt._envoyPerformMode')

    def test_helper_exception_reads_false(self):
        """A broken Embody ext / property must read False -- the watchdog
        keeps self-healing rather than idling forever (fail-open toward
        liveness; the CLASS property is swapped for a raiser and restored)."""
        embody_cls = type(self.embody.ext.Embody)

        def _raiser(_self):
            raise RuntimeError('simulated broken _performMode wire')

        self._patch(embody_cls, '_envoyPerformMode', property(_raiser))

        self.assertFalse(
            self.envoy._performModeActive(),
            'An exception in the Perform authority chain must read as False')


class TestPerformModePollGates(EnvoyWatchdogBase):
    """Perform gates on the three startup polls (panel finding, v6.0.169).

    A start already inside its startup window when Perform Mode is entered
    used to complete mid-show: _enterPerformMode's Stop() no-ops while
    envoy_running is False (the whole window), and _pollImportGate /
    _pollBootstrap / _pollStartup finished the start via _continueStart or
    declared 'Running' -- bypassing the Start() gate entirely. The polls now
    honor the same Perform authority. Perform is stubbed at the instance,
    same rules as TestWatchdogPerformMode.
    """

    _POLL_ATTRS = ('_import_gate_running', '_import_gate_result',
                   '_bootstrapping', '_bootstrap_result',
                   '_startup_event', '_startup_deadline', '_runtime_port',
                   '_last_start_time')   # the declare-Running branch stamps it

    def setUp(self):
        super().setUp()
        self._perform = {'on': True}
        self._patch(self.envoy, '_performModeActive',
                    lambda: self._perform['on'])
        self._continues = []
        self._patch(self.envoy, '_continueStart',
                    lambda git_root: self._continues.append(git_root))
        self._saved_poll_attrs = {
            name: getattr(self.envoy, name, None) for name in self._POLL_ATTRS}

    def tearDown(self):
        for name, val in self._saved_poll_attrs.items():
            setattr(self.envoy, name, val)
        super().tearDown()

    def test_import_gate_poll_aborts_when_performing(self):
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Perform Mode'
        self.envoy._import_gate_running = True
        self.envoy._import_gate_result = (True, 'ok')

        self.envoy._pollImportGate('no-git', {'site_packages': ''})

        self.assertEqual(
            len(self._continues), 0,
            'A warmed import gate must NOT finish the start during Perform')
        self.assertFalse(
            self.envoy._import_gate_running,
            'The poll must still clear its running flag')
        self.assertEqual(
            str(self.embody.par.Envoystatus.eval()), 'Perform Mode',
            'The abort must leave the Perform Mode readout alone')

    def test_bootstrap_poll_aborts_when_performing(self):
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Perform Mode'
        self.envoy._bootstrapping = True
        self.envoy._bootstrap_result = (True, [], True, '')

        self.envoy._pollBootstrap('no-git',
                                  {'site_packages': '', 'venv_dir': ''})

        self.assertEqual(
            len(self._continues), 0,
            'A finished dependency install must NOT finish the start during '
            'Perform')
        self.assertFalse(
            self.envoy._bootstrapping,
            'The poll must still clear its bootstrapping flag')
        self.assertEqual(
            str(self.embody.par.Envoystatus.eval()), 'Perform Mode',
            'The abort must leave the Perform Mode readout alone')

    def test_startup_poll_tears_down_bound_server_when_performing(self):
        """The worker bound while Perform was entering: the poll must shut the
        newborn down (signal ITS shutdown event) instead of declaring
        'Running' over the Perform readout."""
        self.embody.par.Envoyenable = 1
        self.embody.par.Envoystatus = 'Perform Mode'
        self.embody.store('envoy_running', False)
        self.envoy._starting = True
        bound = threading.Event()
        bound.set()
        self.envoy._startup_event = bound

        self.envoy._pollStartup(self.envoy._server_gen)

        self.assertTrue(
            self.envoy.shutdown_event.is_set(),
            'The just-bound worker must be signaled to shut down')
        self.assertFalse(
            self.envoy._starting,
            'The teardown must close the startup window')
        self.assertFalse(
            self.embody.fetch('envoy_running', False, search=False),
            "The teardown must never declare envoy_running")
        self.assertEqual(
            str(self.embody.par.Envoystatus.eval()), 'Perform Mode',
            "The teardown must not clobber the 'Perform Mode' readout")

    def test_startup_poll_declares_running_when_not_performing(self):
        """Passthrough regression: with Perform off, a confirmed bind must
        still declare Running exactly as shipped."""
        self._perform['on'] = False
        self.embody.par.Envoyenable = 1
        self.embody.store('envoy_running', False)
        self.envoy._starting = True
        self.envoy._runtime_port = 65123
        bound = threading.Event()
        bound.set()
        self.envoy._startup_event = bound

        self.envoy._pollStartup(self.envoy._server_gen)

        self.assertTrue(
            self.embody.fetch('envoy_running', False, search=False),
            'A confirmed bind with Perform off must declare envoy_running')
        self.assertEqual(
            str(self.embody.par.Envoystatus.eval()), 'Running on port 65123',
            'A confirmed bind with Perform off must set the Running readout')
        self.assertFalse(self.envoy._starting)


class TestProbeAlive(EnvoyWatchdogBase):
    """_probeAlive against a real stdlib socket listener (no MCP involved)."""

    def setUp(self):
        super().setUp()
        self._listener = None

    def tearDown(self):
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
        super().tearDown()

    def _open_listener(self):
        """Bind an ephemeral localhost listener; return its port."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        s.listen(1)
        self._listener = s
        return s.getsockname()[1]

    def test_probe_true_when_listener_present(self):
        port = self._open_listener()
        self.envoy._runtime_port = port
        self.assertTrue(
            self.envoy._probeAlive(),
            'A live localhost listener on _runtime_port must probe True')

    def test_probe_false_when_listener_closed(self):
        port = self._open_listener()
        # Close the listener so nothing answers on the port.
        self._listener.close()
        self._listener = None
        self.envoy._runtime_port = port
        self.assertFalse(
            self.envoy._probeAlive(),
            'A closed/refused port must probe False')

    def test_probe_true_when_port_unknown(self):
        # Unknown port -> True so the watchdog never restarts on missing info.
        self.envoy._runtime_port = None
        self.assertTrue(
            self.envoy._probeAlive(),
            'A None _runtime_port must probe True (never restart on missing info)')


class TestFindAvailablePort(EnvoyWatchdogBase):
    """_findAvailablePort fast path + force-free branch (helpers monkeypatched).

    The real method defines _port_bindable / _port_registered_by_other /
    _recent_bind_failure as nested closures, so they cannot be patched
    directly. Instead we drive the public inputs: a truly-free ephemeral port
    for the fast path, a busy port plus a recorded _forceCloseOldServer for
    the force-free branch, a bound-but-dead socket for the zombie case, and
    seeded sys._envoy_bad_bind_ports entries for the blacklist.
    """

    def setUp(self):
        super().setUp()
        self._held = []
        # The blacklist is process-global (survives reinit) -- snapshot it so
        # seeded test entries never leak into the live session.
        _sentinel = object()
        self._saved_bad_ports = getattr(sys, '_envoy_bad_bind_ports', _sentinel)
        self._bad_ports_sentinel = _sentinel

    def tearDown(self):
        for s in self._held:
            try:
                s.close()
            except Exception:
                pass
        self._held = []
        if self._saved_bad_ports is self._bad_ports_sentinel:
            try:
                del sys._envoy_bad_bind_ports
            except AttributeError:
                pass
        else:
            sys._envoy_bad_bind_ports = self._saved_bad_ports
        super().tearDown()

    def _free_port(self):
        """Bind+release an ephemeral port to learn a number that is free now."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _hold_port(self):
        """Bind+listen on an ephemeral port and keep it held; return the port."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        s.listen(1)
        self._held.append(s)
        return s.getsockname()[1]

    def test_fast_path_returns_free_base_port(self):
        # No registry entry exists for a random ephemeral port and nothing is
        # listening on it -> the fast path returns it unchanged.
        base = self._free_port()
        # Use a wide range so a transient collision still resolves to a port.
        result = self.envoy._findAvailablePort(base, range_size=10)
        self.assertIsNotNone(
            result, 'A free base port must resolve to a usable port')
        self.assertIsInstance(result, int)

    def test_force_free_branch_invoked_when_base_busy(self):
        # Hold the base port so it is genuinely in use (but NOT registered by
        # another instance), which routes through the force-close branch.
        busy = self._hold_port()
        called = {'n': 0}

        def fake_force_close():
            called['n'] += 1
            return False  # nothing of ours was holding it -> fall to range scan

        self._patch(self.envoy, '_forceCloseOldServer', fake_force_close)

        result = self.envoy._findAvailablePort(busy, range_size=10)

        self.assertGreaterEqual(
            called['n'], 1,
            'A busy, non-foreign base port must attempt a force-close')
        # The held base is busy; the scan should hand back a different port,
        # or None if the whole range is somehow occupied. Either is valid; we
        # only assert it never returns the still-held base port.
        if result is not None:
            self.assertNotEqual(
                result, busy,
                'Must not return the still-held base port')

    def test_force_freed_base_port_reused(self):
        # Simulate force-close actually freeing the port: hold it, then have
        # the patched _forceCloseOldServer release it and report success so the
        # drain-wait re-checks and reuses the SAME base port.
        busy = self._hold_port()
        held_sock = self._held[-1]

        def fake_force_close():
            try:
                held_sock.close()
            except Exception:
                pass
            return True  # we "owned" it; drain-wait will re-check the base port

        self._patch(self.envoy, '_forceCloseOldServer', fake_force_close)

        result = self.envoy._findAvailablePort(busy, range_size=10)
        self.assertEqual(
            result, busy,
            'After force-close frees the base port, it must be reused (no drift)')

    def _zombie_port(self):
        """Bind WITHOUT listen and keep held: the dead-listener signature.

        Connects to such a port are REFUSED (the old connect probe reported
        it free) while bind still fails with EADDRINUSE/WinError 10048 --
        exactly how the 2026-07-23 windowless zombie TD poisoned port 9872.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 0))
        self._held.append(s)
        return s.getsockname()[1]

    def test_zombie_bound_port_never_returned(self):
        # The scanner must not hand back a port that cannot be bound, even
        # though nothing accepts connections on it (connect probe blind spot).
        zombie = self._zombie_port()
        self._patch(self.envoy, '_forceCloseOldServer', lambda: False)

        result = self.envoy._findAvailablePort(zombie, range_size=10)

        self.assertIsNotNone(
            result, 'A free port must exist above the zombie-held base')
        self.assertNotEqual(
            result, zombie,
            'A bound-but-dead (zombie) port must never be selected: its '
            'connects are refused but a real bind on it fails')

    def test_recent_bind_failure_blacklist_skips_port(self):
        # A port whose last uvicorn bind failed within the TTL is skipped even
        # though it probes free right now (probe/bind race defense).
        free = self._free_port()
        self._patch(self.envoy, '_forceCloseOldServer', lambda: False)
        sys._envoy_bad_bind_ports = {free: time.time()}

        result = self.envoy._findAvailablePort(free, range_size=10)

        self.assertNotEqual(
            result, free,
            'A port with a fresh bind-failure record must be skipped')

    def test_expired_bind_failure_entry_ignored_and_pruned(self):
        free = self._free_port()
        stale = time.time() - self.envoy._BIND_FAIL_TTL_SECONDS - 1
        sys._envoy_bad_bind_ports = {free: stale}

        result = self.envoy._findAvailablePort(free, range_size=10)

        self.assertEqual(
            result, free,
            'An expired bind-failure record must not block the port')
        self.assertNotIn(
            free, sys._envoy_bad_bind_ports,
            'An expired bind-failure record must be pruned on lookup')


class TestBindFailureBlacklistLifecycle(EnvoyWatchdogBase):
    """The record/clear sides of the bind-failure blacklist.

    _onServerError records the runtime port when the worker died WITHOUT ever
    confirming a bind; _pollStartup drops the entry once a real bind is
    confirmed (a stale late error from an older generation must not poison a
    healthy port for the TTL).
    """

    _FAKE_PORT = 55555  # never bound by these tests -- pure bookkeeping

    def setUp(self):
        super().setUp()
        _sentinel = object()
        self._saved_bad_ports = getattr(sys, '_envoy_bad_bind_ports', _sentinel)
        self._bad_ports_sentinel = _sentinel
        self._saved_task = getattr(self.envoy, 'current_task', None)
        self._saved_last_start = getattr(self.envoy, '_last_start_time', 0.0)
        # _onServerError escalates into _scheduleRestart (backoff counters,
        # status churn, queued run()) -- record instead of executing.
        self._restarts = []
        self._patch(self.envoy, '_scheduleRestart',
                    lambda reason: self._restarts.append(reason))

    def tearDown(self):
        self.envoy.current_task = self._saved_task
        self.envoy._last_start_time = self._saved_last_start
        if self._saved_bad_ports is self._bad_ports_sentinel:
            try:
                del sys._envoy_bad_bind_ports
            except AttributeError:
                pass
        else:
            sys._envoy_bad_bind_ports = self._saved_bad_ports
        super().tearDown()

    def test_prebind_death_records_port(self):
        sys._envoy_bad_bind_ports = {}
        never_bound = threading.Event()  # NOT set -> worker never bound
        self._patch(self.envoy, '_startup_event', never_bound)
        self._patch(self.envoy, '_runtime_port', self._FAKE_PORT)

        self.envoy._onServerError('bind exploded (test)')

        self.assertIn(
            self._FAKE_PORT, sys._envoy_bad_bind_ports,
            'A worker death before the bind confirmation must blacklist '
            'its port so the restart scans past it')
        self.assertAlmostEqual(
            sys._envoy_bad_bind_ports[self._FAKE_PORT], time.time(), delta=5,
            msg='The blacklist entry must carry a fresh timestamp')

    def test_postbind_death_does_not_record_port(self):
        sys._envoy_bad_bind_ports = {}
        bound = threading.Event()
        bound.set()  # worker HAD confirmed a bind -> port itself is fine
        self._patch(self.envoy, '_startup_event', bound)
        self._patch(self.envoy, '_runtime_port', self._FAKE_PORT)

        self.envoy._onServerError('died after serving (test)')

        self.assertNotIn(
            self._FAKE_PORT, sys._envoy_bad_bind_ports,
            'A death AFTER a confirmed bind is not a port problem -- the '
            'port must stay eligible for the restart')

    def test_confirmed_bind_clears_blacklist_entry(self):
        sys._envoy_bad_bind_ports = {self._FAKE_PORT: time.time()}
        bound = threading.Event()
        bound.set()
        self._patch(self.envoy, '_startup_event', bound)
        self._patch(self.envoy, '_runtime_port', self._FAKE_PORT)
        self._patch(self.envoy, '_starting', True)

        self.envoy._pollStartup(self.envoy._server_gen)

        self.assertNotIn(
            self._FAKE_PORT, sys._envoy_bad_bind_ports,
            'A confirmed bind proves the port healthy -- a stale blacklist '
            'entry for it (late error from an older generation) must clear')


class TestRunTestsStatusRestore(EmbodyTestCase):
    """The MCP _run_tests Status='Testing' stomp: the prior Status survives in
    COMP storage (reinit-proof) and _restoreStatusAfterTests is idempotent.

    These tests may themselves run INSIDE a live stomp window (invoked via MCP
    run_tests, where Status=='Testing' and storage holds the real prior), so
    setUp snapshots the live par + storage + instance attribute and tearDown
    restores all three exactly -- the enclosing run's own restore still works.
    """

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self._saved_live = self.embody.par.Status.eval()
        self._saved_store = self.embody.fetch(
            '_test_saved_status', None, search=False)
        self._saved_attr = getattr(self.envoy, '_test_saved_status', None)

    def tearDown(self):
        if self._saved_store is None:
            self.embody.unstore('_test_saved_status')
        else:
            self.embody.store('_test_saved_status', self._saved_store)
        self.envoy._test_saved_status = self._saved_attr
        self.embody.par.Status = self._saved_live
        super().tearDown()

    def test_restore_reads_comp_storage(self):
        """The stored prior wins, is applied, and is cleared after restore --
        the reinit-proof path (an instance attribute would have been wiped)."""
        self.embody.store('_test_saved_status', 'Enabled')
        self.envoy._test_saved_status = None
        self.embody.par.Status = 'Testing'

        self.envoy._restoreStatusAfterTests()

        self.assertEqual(self.embody.par.Status.eval(), 'Enabled',
                         'Restore must apply the storage-backed prior Status')
        self.assertIsNone(
            self.embody.fetch('_test_saved_status', None, search=False),
            'Restore must clear the stored prior (idempotent re-entry)')

    def test_restore_falls_back_to_instance_attr(self):
        """A legacy pre-hardening save on the instance still restores."""
        self.embody.unstore('_test_saved_status')
        self.envoy._test_saved_status = 'Enabled'
        self.embody.par.Status = 'Testing'

        self.envoy._restoreStatusAfterTests()

        self.assertEqual(self.embody.par.Status.eval(), 'Enabled',
                         'Restore must fall back to the legacy instance attr')
        self.assertIsNone(self.envoy._test_saved_status,
                          'Restore must clear the legacy instance attr')

    def test_restore_noop_when_nothing_saved(self):
        """No saved prior anywhere -> leave Status alone (a stuck 'Testing'
        must stay visible and fail loud upstream, never be invented over)."""
        self.embody.unstore('_test_saved_status')
        self.envoy._test_saved_status = None
        self.embody.par.Status = 'Testing'

        self.envoy._restoreStatusAfterTests()

        self.assertEqual(self.embody.par.Status.eval(), 'Testing',
                         'With nothing saved, restore must not touch Status')
