"""
Test suite: the release smoke's `upgrade` leg (smoke_legs/upgrade.py).

Pure Python, TD-import-free: the leg is orchestrator-side code, loaded by
file path and driven against a scripted stand-in for the smoke TD on a fake
clock, so these run under pytest on the windows/macos bridge matrix. Inside
TD every test skips.

The fake moves each of the leg's witnesses INDEPENDENTLY -- the EmbodyExt op
id, the extension-source fingerprint, par.Version, the per-DAT map and the
sentinel -- because a fake that swings them together lets every identity
assertion be deleted with the suite still green (mutation run, 2026-09-19).

What this pins:
- the happy path: seed -> ApplyUpdate -> verified new version -> rollback ->
  re-update, every step ok, and ctx['installed'] tracking the running build;
- a swap that moves version and fingerprint but NOT the op id is not a
  reload, in either direction, and neither is a rollback that leaves the
  sentinel behind;
- identity is decided on the DATs that held still, so a release that touches
  no extension source still passes and a component that changed nothing (only
  its log FIFO moved) fails;
- the swap moves Envoy's port, and the leg re-reads it from
  .embody/envoy.json and calls set_port (its calls would otherwise hit a
  dead port forever);
- a swap that never lands fails with the status the leg waited for, inside
  the budget, instead of hanging;
- a port that answers for someone else's project is never believed (a swap
  frees a port, and another TouchDesigner can take it);
- errors introduced by the update fail the leg, a transient Envoy status
  after a swap does not, and an unreadable externalization table is not a
  pass;
- the busy latch: waited out, retried when it re-arms between the probe and
  the apply, reported when it never clears;
- no budget for the re-update is a FAILURE (the project would be left on the
  previous release for the legs that follow);
- run() never raises: a transport that throws comes back as ok=False.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_IN_TD = 'td' in sys.modules

_DEV = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_RT = os.path.join(_DEV, 'release_testing')

_OLD = '6.2.56'
_NEW = '6.2.57'
_FP_OLD = 'a' * 64
_FP_NEW = 'b' * 64
_ASSET = f'Embody-v{_NEW}.tox'

# The component's DATs. '/fifo1' is NOT here: it is the log FIFO the fake
# moves on every probe, the runtime noise the leg's identity check filters.
_DATS_OLD = {'/EmbodyExt': 'a1', '/EnvoyExt': 'a2', '/TDXNExt': 'a3',
             '/templates/rule': 'a4', '/updater/UpdaterExt': 'a5'}
_DATS_NEW = dict(_DATS_OLD, **{'/EmbodyExt': 'b1', '/templates/rule': 'b4'})


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if not _IN_TD:
    upgrade = _load('smoke_leg_upgrade_under_test',
                    os.path.join(_RT, 'smoke_legs', 'upgrade.py'))


class _Clock:
    """Fake monotonic; the leg's sleep() is this advance (CI runners stall,
    and a real-clock deadline test fails wherever the stall lands)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _FakeTD:
    """The smoke instance: answers on ONE port, and a swap takes it down,
    moves it, and re-writes the registry -- the shape the leg must survive.
    Every witness a swap can move is its own attribute so a test can move
    one and freeze the rest."""

    def __init__(self, clock, run_dir, updates_dir, port=9870):
        self.clock = clock
        self.run_dir = run_dir
        self.sentinel = os.path.join(updates_dir, 'pending.json')
        self.port = port
        self.seeds = []
        self.applies = []
        self.down_until = 0.0
        self.swap_at = None
        self.swap_to = None
        self.busy_until = 0.0   # the startup check the swap wakes up
        self.busy_for = 4.0
        self.busy_traps = 0     # latches armed between a probe and an apply
        self.errors = 0
        self.ext_count = 3
        self.log_n = 0
        self.settle_at = 0.0
        self.envoy_settle_s = 0.0
        self.dats = dict(_DATS_OLD)
        # What each swap does, independently switchable.
        self.update_changes = {'version': _NEW, 'fingerprint': _FP_NEW}
        self.update_dats = dict(_DATS_NEW)
        self.update_token = True
        self.update_clear = True
        self.rollback_changes = {'fingerprint': _FP_OLD}
        self.rollback_dats = dict(_DATS_OLD)
        self.rollback_token = True
        self.rollback_clear = True
        self.state = {
            'path': '/Embody', 'folder': run_dir, 'token': 100,
            'version': _OLD, 'status': 'Enabled',
            'envoy': f'Running on port {port}',
            'update_status': f'v{_NEW} available',
            'autoupdate': 'notify', 'fingerprint': _FP_OLD,
            'updates_dir': updates_dir, 'ready': True,
            'min_backup': 100_000, 'busy': False, 'busy_phase': '',
        }
        self._write_registry()

    # -- harness ----------------------------------------------------------

    def _write_registry(self):
        path = os.path.join(self.run_dir, '.embody', 'envoy.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'active': 'smoke', 'instances': {
                'smoke': {'toe_path': 'smoke.toe', 'port': self.port,
                          'td_pid': 4242}}}, f)

    def _swap(self, to, dats, port=None, token=True, clear=True,
              down=6.0, took=10.0):
        """Queue a swap: Envoy dies now, the component becomes `to` later,
        the sentinel appears now and is cleared when the swap verifies."""
        self.down_until = self.clock() + down
        self.swap_at = self.clock() + took
        self.swap_to = (dict(to), dict(dats), port, token, clear)
        with open(self.sentinel, 'w', encoding='utf-8') as f:
            f.write('{}')

    def _tick(self):
        if self.swap_at is not None and self.clock() >= self.swap_at:
            changes, dats, port, token, clear = self.swap_to
            self.swap_at = None
            self.swap_to = None
            if port:
                self.port = port
            self._write_registry()
            self.state.update(changes)
            self.dats = dict(dats)
            if token:   # a REAL reload recreates EmbodyExt (a new op id)
                self.state['token'] += 1
            # The reloaded component's own notify-mode startup check
            # overwrites VerifyUpdate's / VerifyRollback's line within
            # seconds -- the live behaviour that broke the first witness.
            self.state['update_status'] = \
                f"Up to date (v{self.state['version']})"
            self.state['envoy'] = f'Running on port {self.port}'
            if self.envoy_settle_s:  # the bind is not confirmed yet
                self.state['envoy'] = 'Starting Envoy MCP server...'
                self.settle_at = self.clock() + self.envoy_settle_s
            self.busy_until = self.clock() + self.busy_for
            if clear and os.path.isfile(self.sentinel):
                os.unlink(self.sentinel)
        if self.settle_at and self.clock() >= self.settle_at:
            self.settle_at = 0.0
            self.state['envoy'] = f'Running on port {self.port}'
        busy = self.clock() < self.busy_until
        self.state['busy'] = busy
        self.state['busy_phase'] = 'check' if busy else ''

    def _live(self, port):
        self._tick()
        if self.clock() < self.down_until or int(port) != self.port:
            raise RuntimeError(f'connection refused on port {port}')

    # -- the two ctx transports ------------------------------------------

    def call(self, port, name, arguments):
        self._live(port)
        if name == 'get_op_errors':
            return {'path': '/Embody', 'errorCount': self.errors,
                    'errors': [], 'warnings': []}
        if name == 'get_externalizations':
            return {'count': self.ext_count, 'externalizations': []}
        raise RuntimeError(f'{name}: unscripted tool')

    def py(self, port, code):
        self._live(port)
        if '_smoke_test_responses' in code:
            self.seeds.append(code)
            return self.state['path']
        if 'ApplyUpdate' in code:
            if self.state['busy'] or self.busy_traps:  # _busyBlocks
                if self.busy_traps:
                    self.busy_traps -= 1
                    self.busy_until = self.clock() + 3.0
                return json.dumps(
                    {'error': 'An update is already running (check).'})
            self.applies.append(code)
            self._swap(self.update_changes, self.update_dats,
                       port=self.port + 1, token=self.update_token,
                       clear=self.update_clear)
            return json.dumps({'status': 'applying'})
        if '_finishCheck' in code:
            self.state['update_status'] = f"Up to date (v{self.state['version']})"
            return self.state['update_status']
        if '_writeSentinel' in code:
            self._swap(self.rollback_changes, self.rollback_dats,
                       token=self.rollback_token, clear=self.rollback_clear)
            return 'armed'
        self.log_n += 1   # the log FIFO moves on its own, every read
        snap = dict(self.state)
        snap['dats'] = dict(self.dats, **{'/fifo1': f'log{self.log_n}'})
        return json.dumps(snap)


class _Case(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only')
        self.root = tempfile.mkdtemp(prefix='embody_smoke_upgrade_')
        self.run_dir = os.path.join(self.root, 'run')
        self.updates = os.path.join(self.run_dir, '.embody', 'updates')
        os.makedirs(self.updates)
        # A tox the leg can really copy and hash, and the backup the
        # updater would have exported beside it.
        self.tox = os.path.join(self.root, 'repo', 'release', _ASSET)
        os.makedirs(os.path.dirname(self.tox))
        payload = b'tox' * 4000
        with open(self.tox, 'wb') as f:
            f.write(payload)
        with open(os.path.join(self.updates, f'backup-v{_OLD}.tox'),
                  'wb') as f:
            f.write(b'backup' * 20000)
        self.digest = hashlib.sha256(payload).hexdigest()
        with open(os.path.join(self.root, 'repo', 'release',
                               'embody-release.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'version': _NEW, 'tag': f'v{_NEW}', 'asset': _ASSET,
                       'size': len(payload), 'sha256': self.digest,
                       'min_td_build': '2025.33230'}, f)
        self.clock = _Clock()
        self.td = _FakeTD(self.clock, self.run_dir, self.updates)
        self.ports = []

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def _ctx(self, budget=600.0, py=None, call=None):
        td = self.td
        live = {'port': td.port}
        start = self.clock()

        def set_port(p):
            live['port'] = int(p)
            self.ports.append(int(p))

        return {
            'run_dir': self.run_dir,
            'repo': os.path.join(self.root, 'repo'),
            'build': {'tox': self.tox, 'version': _NEW, 'sha256': self.digest,
                      'size': os.path.getsize(self.tox),
                      'td_build': '2025.33230'},
            'installed': {'tox': 'old.tox', 'version': _OLD},
            'upgrade_from': {'tox': 'old.tox', 'version': _OLD,
                             'tag': f'v{_OLD}'},
            'port': live['port'], 'set_port': set_port, 'pid': 4242,
            'td_exe': 'TouchDesigner.exe', 'platform': 'win32',
            'call': call or (lambda n, a, t=30: td.call(live['port'], n, a)),
            'py': py or (lambda c, t=30: td.py(live['port'], c)),
            'log': lambda m: None,
            'budget': lambda: budget - (self.clock() - start),
            'wait_for_flag': lambda n, t, d: ('', False),
        }

    def _run(self, ctx=None):
        return upgrade.run(ctx or self._ctx(), clock=self.clock,
                           sleep=self.clock.advance)

    def _step(self, res, name):
        for step in res['steps']:
            if step['step'] == name:
                return step
        self.fail(f'no {name!r} step in {[s["step"] for s in res["steps"]]}')


class TestHappyPath(_Case):

    def test_every_step_passes(self):
        res = self._run()
        bad = [s for s in res['steps'] if not s['ok']]
        self.assertEqual(bad, [], f'failed steps: {bad}; error={res["error"]}')
        self.assertTrue(res['ok'])
        self.assertEqual(res['error'], '')
        for name in ('installed_version', 'dialog_guard', 'manifest',
                     'stage_update', 'apply_update', 'verify_update',
                     'update_identity', 'update_health', 'externalizations',
                     'up_to_date', 'backup', 'rollback_trigger', 'rollback',
                     'rollback_identity', 'rollback_health', 'stage_reupdate',
                     'apply_reupdate', 'verify_reupdate', 'reupdate_identity',
                     'reupdate_health', 'build_restored'):
            self.assertTrue(self._step(res, name)['ok'], name)

    def test_staged_tox_lands_in_the_updates_dir(self):
        """Never the repo asset: the updater unlinks what it installed."""
        self._run()
        staged = os.path.join(self.updates, _ASSET)
        self.assertTrue(os.path.isfile(staged))
        self.assertTrue(os.path.isfile(self.tox), 'repo asset was consumed')

    def test_apply_is_driven_through_the_product_api(self):
        self._run()
        self.assertEqual(len(self.td.applies), 2, 'update + re-update')
        self.assertIn('ApplyUpdate(interactive=False)', self.td.applies[0])
        self.assertIn('_pending', self.td.applies[0])
        self.assertIn('validateManifest', self.td.applies[0],
                      'the shipped manifest goes through the product gate')

    def test_ctx_installed_tracks_the_build_that_is_running(self):
        """The legs that follow read this dict to describe the build."""
        ctx = self._ctx()
        self._run(ctx)
        self.assertEqual(ctx['installed'], {'tox': self.tox, 'version': _NEW})

    def test_dialog_guard_merges_and_only_restores(self):
        """A swap wipes storage; an unseeded modal would freeze the smoke.
        The seed must MERGE (the bootstrap's install answers live in the
        same store) and the re-arms must only fire while it is ABSENT --
        re-storing a consumed 'Embody Update' answer would answer Install
        in a later check dialog."""
        res = self._run()
        self.assertTrue(res['ok'], res['error'])
        self.assertGreaterEqual(len(self.td.seeds), 3)
        self.assertIn('Embody Update', self.td.seeds[1],
                      'the rollback needs the Restore Backup answer seeded')
        for code in self.td.seeds:
            self.assertIn("fetch('_smoke_test_responses', None, search=False)",
                          code, 'the seed overwrote the store instead of '
                                'merging into it')
            self.assertIn('is None else None', code,
                          'the re-arm is not restorative-only')
            self.assertIn('delayFrames', code,
                          'nothing re-arms the seed after a swap wipes it')


class TestPortReread(_Case):

    def test_the_leg_follows_envoy_to_its_new_port(self):
        res = self._run()
        self.assertTrue(res['ok'], res['error'])
        self.assertIn(9871, self.ports)
        self.assertIn('9871', self._step(res, 'verify_update')['detail'])

    def test_a_registry_that_never_moves_is_a_timeout_not_a_hang(self):
        self.td._write_registry = lambda: None  # registry frozen on 9870
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('verify_update', res['error'])

    def test_registry_port_prefers_this_td_pid(self):
        path = os.path.join(self.run_dir, '.embody', 'envoy.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'active': 'other', 'instances': {
                'other': {'port': 9000, 'td_pid': 1},
                'smoke': {'port': 9876, 'td_pid': 4242}}}, f)
        self.assertEqual(upgrade.registry_port(self.run_dir, 4242), 9876)
        self.assertEqual(upgrade.registry_port(self.run_dir, None), 9000)
        self.assertIsNone(upgrade.registry_port(self.root, 4242))

    def test_the_entry_probe_is_retried(self):
        """The MCP probe phase runs just before the leg; one transient miss
        at entry must not abort it."""
        self.td.down_until = self.clock() + 5.0
        res = self._run()
        self.assertTrue(res['ok'], res['error'])


class TestIdentity(_Case):
    """The leg's headline claim: the component really became the other
    build. Each witness moves on its own here."""

    def test_a_swap_that_does_not_reload_fails_verify(self):
        """par.Version and the sources moved but the EmbodyExt op id did
        not -- a stamp without a reload, the lie the reload token exists to
        catch."""
        self.td.update_token = False
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('verify_update', res['error'])

    def test_a_rollback_that_does_not_reload_fails(self):
        self.td.rollback_token = False
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('rollback', res['error'])
        self.assertIn('never reloaded', res['error'])

    def test_a_rollback_that_never_clears_the_sentinel_fails(self):
        """VerifyRollback deletes pending.json only after it confirms the
        reload; a sentinel left behind is an unfinished rollback."""
        self.td.rollback_clear = False
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('rollback', res['error'])

    def test_a_release_that_touches_no_extension_source_still_passes(self):
        """v6.2.50 -> v6.2.51 changed no extension DAT. Deciding identity on
        those three alone would fail a healthy release -- a false red on a
        gate that blocks releases."""
        self.td.update_changes = {'version': _NEW}      # fingerprint unmoved
        self.td.rollback_changes = {}
        res = self._run()
        self.assertTrue(res['ok'], res['error'])
        self.assertIn('stable DATs changed',
                      self._step(res, 'update_identity')['detail'])

    def test_a_component_whose_dats_never_changed_fails(self):
        """Only the log FIFO moved. Counting runtime noise as evidence would
        make the identity step pass on any reload at all."""
        self.td.update_dats = dict(_DATS_OLD)
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('update_identity', res['error'])
        self.assertIn('0/', self._step(res, 'update_identity')['detail'])

    def test_rollback_that_restores_the_wrong_component_fails(self):
        """The swap happens (a new op id, the sentinel clears) but what came
        back is not the pre-update build."""
        self.td.rollback_changes = {'fingerprint': 'c' * 64}
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('rollback_identity', res['error'])
        self.assertFalse(self._step(res, 'rollback_identity')['ok'])

    def test_a_reupdate_that_comes_back_as_the_old_build_fails(self):
        """Phase 2 fails and VerifyUpdate restores the backup: the op id
        moves, the sentinel clears, par.Version still reads NEW."""
        real = self.td.py

        def py(port, code):
            if 'ApplyUpdate' in code and len(self.td.applies) == 1:
                self.td.update_changes = {'version': _NEW,
                                          'fingerprint': _FP_OLD}
                self.td.update_dats = dict(_DATS_OLD)
            return real(port, code)
        self.td.py = py
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('reupdate_identity', res['error'])


class TestFailureModes(_Case):

    def test_update_never_lands(self):
        self.td._swap = lambda *a, **k: None  # ApplyUpdate answers, nothing swaps
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('verify_update', res['error'])
        self.assertIn(f'verified v{_NEW}', res['error'])
        step = self._step(res, 'verify_update')
        self.assertFalse(step['ok'])
        self.assertEqual([s['step'] for s in res['steps']][-1], 'verify_update',
                         'the leg must stop at the first failure')

    def test_budget_exhaustion_is_named(self):
        """Under the teardown reserve the leg says so, not 'timed out'."""
        res = self._run(self._ctx(budget=20.0))
        self.assertFalse(res['ok'])
        self.assertIn('no budget left', res['error'])

    def test_no_budget_for_the_reupdate_is_a_failure(self):
        """Passing here would report the gate green for a project left on
        the PREVIOUS release, which the legs after this one would test."""
        ctx = self._ctx(budget=120.0)
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('reupdate', res['error'])
        self.assertTrue(self._step(res, 'rollback')['ok'])
        self.assertEqual(len(self.td.applies), 1, 'nothing was re-applied')
        self.assertEqual(ctx['installed']['version'], _OLD,
                         'ctx must name the build actually running')

    def test_errors_after_the_update_fail_the_leg(self):
        real = self.td._tick

        def tick():
            was = self.td.state['version']
            real()
            if was != self.td.state['version']:
                self.td.errors = 3
        self.td._tick = tick
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('update_health', res['error'])
        self.assertIn('errorCount=3', self._step(res, 'update_health')['detail'])

    def test_an_unreadable_error_count_is_not_a_pass(self):
        ctx = self._ctx()
        base = ctx['call']

        def call(name, arguments, timeout=30):
            if name == 'get_op_errors' and self.td.applies:
                raise RuntimeError('get_op_errors: Envoy is restarting')
            return base(name, arguments, timeout)
        ctx['call'] = call
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('update_health', res['error'])

    def test_an_unreadable_externalization_table_is_not_a_pass(self):
        """None == None must never read as 'the table is unchanged'."""
        ctx = self._ctx()
        base = ctx['call']

        def call(name, arguments, timeout=30):
            if name == 'get_externalizations':
                raise RuntimeError('get_externalizations: table locked')
            return base(name, arguments, timeout)
        ctx['call'] = call
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('externalizations', res['error'])

    def test_a_transient_envoy_status_after_a_swap_settles(self):
        """EnvoyExt writes 'Running on port N' only once the bind is
        confirmed; one sample of the revive window must not go red."""
        self.td.envoy_settle_s = 6.0
        res = self._run()
        self.assertTrue(res['ok'], res['error'])

    def test_an_envoy_that_never_comes_back_fails_health(self):
        self.td.envoy_settle_s = 10_000.0
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('update_health', res['error'])

    def test_a_swap_that_never_verifies_fails(self):
        """The component comes back but VerifyUpdate never cleared its
        sentinel -- an update that did not finish is not an update."""
        real = self.td._tick

        def tick():
            real()
            with open(self.td.sentinel, 'w', encoding='utf-8') as f:
                f.write('{"phase": "reloading"}')
        self.td._tick = tick
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('verify_update', res['error'])

    def test_manifest_that_does_not_describe_the_build_is_refused(self):
        ctx = self._ctx()
        ctx['build']['sha256'] = 'f' * 64
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('sha256', res['error'])
        self.assertEqual(len(self.td.applies), 0, 'nothing was installed')

    def test_nothing_to_upgrade(self):
        ctx = self._ctx()
        ctx['installed'] = {'tox': 'x.tox', 'version': _NEW}
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('preconditions', res['error'])

    def test_an_updater_stuck_busy_fails_the_apply(self):
        """The happy path waits the startup check out; a latch that never
        clears is reported, not retried forever."""
        self.td.busy_for = 10_000.0
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('apply_reupdate', res['error'])
        self.assertIn('busy', res['error'])

    def test_a_latch_that_re_arms_between_the_probe_and_the_apply_is_retried(self):
        """The wait and the apply are two round trips; the notify-mode check
        can arm in between, and that refusal is not a product failure."""
        self.td.busy_traps = 1
        res = self._run()
        self.assertTrue(res['ok'], res['error'])
        self.assertIn('attempt 2', self._step(res, 'apply_update')['detail'])

    def test_an_apply_refused_for_another_reason_is_not_retried(self):
        ctx = self._ctx()
        base = ctx['py']

        def py(code, timeout=30):
            if 'ApplyUpdate' in code:
                return json.dumps({'error': 'Dev checkout -- refusing '
                                            'self-update.'})
            return base(code, timeout)
        ctx['py'] = py
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('apply_update', res['error'])
        self.assertIn('Dev checkout', res['error'])

    def test_a_backup_below_the_product_floor_fails(self):
        """The rollback artifact IS the recovery point."""
        with open(os.path.join(self.updates, f'backup-v{_OLD}.tox'),
                  'wb') as f:
            f.write(b'tiny')
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('backup', res['error'])

    def test_the_backup_floor_comes_from_the_live_updater(self):
        """_MIN_BACKUP_BYTES is probed, not duplicated -- a product floor
        the leg does not track would accept a backup the product rejects."""
        self.td.state['min_backup'] = 10 ** 9
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('backup', res['error'])
        self.assertIn('floor 1000000000', self._step(res, 'backup')['detail'])

    def test_a_staged_tox_that_does_not_match_the_manifest_fails(self):
        """What lands in .embody/updates is what the product will install."""
        ctx = self._ctx()
        with open(self.tox, 'wb') as f:
            f.write(b'x' * len(b'tox' * 4000))   # same size, other bytes
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('stage_update', res['error'])
        self.assertEqual(len(self.td.applies), 0, 'nothing was installed')

    def test_a_manifest_asset_outside_the_run_dir_is_refused(self):
        """`asset` flows into a path; the smoke writes only under run_dir."""
        path = os.path.join(self.root, 'repo', 'release',
                            'embody-release.json')
        with open(path, encoding='utf-8') as f:
            manifest = json.load(f)
        manifest['asset'] = '../../../../evil.tox'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('outside the run directory', res['error'])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'evil.tox')))

    def test_a_component_on_another_version_fails_the_precondition(self):
        """The leg must start on the PREVIOUS release, not whatever booted."""
        self.td.state['version'] = '6.2.40'
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('installed_version', res['error'])
        self.assertEqual(len(self.td.applies), 0, 'nothing was installed')

    def test_a_check_that_does_not_read_up_to_date_fails(self):
        ctx = self._ctx()
        base = ctx['py']

        def py(code, timeout=30):
            if '_finishCheck' in code:
                return 'Checking for updates...'
            return base(code, timeout)
        ctx['py'] = py
        res = self._run(ctx)
        self.assertFalse(res['ok'])
        self.assertIn('up_to_date', res['error'])

    def test_a_swap_that_never_stamps_the_version_fails_verify(self):
        """VerifyUpdate stamps par.Version; a reload that did not get there
        is not a verified update."""
        self.td.update_changes = {'fingerprint': _FP_NEW}
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('verify_update', res['error'])

    def test_a_foreign_project_on_the_port_is_not_believed(self):
        self.td.state['folder'] = 'C:/Users/dev/Documents/Embody'
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('not the run dir', res['error'])
        self.assertEqual(len(self.td.applies), 0, 'nothing was installed')

    def test_a_throwing_transport_never_escapes_run(self):
        def boom(code, timeout=30):
            raise RuntimeError('MCP call failed: connection reset')
        res = self._run(self._ctx(py=boom))
        self.assertFalse(res['ok'])
        self.assertIn('Envoy did not answer', res['error'])
        self.assertIsInstance(res['steps'], list)
