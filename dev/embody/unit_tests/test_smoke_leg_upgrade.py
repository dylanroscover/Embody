"""
Test suite: the release smoke's `upgrade` leg (smoke_legs/upgrade.py).

Pure Python, TD-import-free: the leg is orchestrator-side code, loaded by
file path and driven against a scripted stand-in for the smoke TD on a fake
clock, so these run under pytest on the windows/macos bridge matrix. Inside
TD every test skips.

What this pins:
- the happy path: seed -> ApplyUpdate -> verified new version -> rollback ->
  re-update, every step ok;
- the swap moves Envoy's port, and the leg re-reads it from
  .embody/envoy.json and calls set_port (its calls would otherwise hit a
  dead port forever);
- a swap that never lands fails with the status the leg waited for, inside
  the budget, instead of hanging;
- a port that answers for someone else's project is never believed (a swap
  frees a port, and another TouchDesigner can take it);
- errors introduced by the update fail the leg (an update that boots dirty
  is not an update that worked);
- a rollback that does not restore the pre-update component fails;
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
    moves it, and re-writes the registry -- the shape the leg must survive."""

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
        self.errors = 0
        self.ext_count = 3
        self.state = {
            'path': '/Embody', 'folder': run_dir, 'token': 100,
            'version': _OLD, 'status': 'Enabled',
            'envoy': f'Running on port {port}',
            'update_status': f'v{_NEW} available',
            'autoupdate': 'notify', 'fingerprint': _FP_OLD,
            'updates_dir': updates_dir, 'ready': True,
            'busy': False, 'busy_phase': '',
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

    def _swap(self, to, port=None, down=6.0, took=10.0):
        """Queue a swap: Envoy dies now, the component becomes `to` later,
        the sentinel appears now and is cleared when the swap verifies."""
        self.down_until = self.clock() + down
        self.swap_at = self.clock() + took
        self.swap_to = (dict(to), port)
        with open(self.sentinel, 'w', encoding='utf-8') as f:
            f.write('{}')

    def _tick(self):
        if self.swap_at is not None and self.clock() >= self.swap_at:
            changes, port = self.swap_to
            self.swap_at = None
            self.swap_to = None
            if port:
                self.port = port
                changes.setdefault('envoy', f'Running on port {port}')
            self._write_registry()
            self.state.update(changes)
            self.state['token'] += 1
            # The reloaded component's own notify-mode startup check
            # overwrites VerifyUpdate's / VerifyRollback's line within
            # seconds -- the live behaviour that broke the first witness.
            self.state['update_status'] = \
                f"Up to date (v{self.state['version']})"
            self.busy_until = self.clock() + self.busy_for
            if os.path.isfile(self.sentinel):
                os.unlink(self.sentinel)
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
            if self.state['busy']:  # UpdaterExt._busyBlocks
                return json.dumps(
                    {'error': 'An update is already running (check).'})
            self.applies.append(code)
            self._swap({'version': _NEW, 'fingerprint': _FP_NEW},
                       port=self.port + 1)
            return json.dumps({'status': 'applying'})
        if '_finishCheck' in code:
            self.state['update_status'] = f"Up to date (v{self.state['version']})"
            return self.state['update_status']
        if '_writeSentinel' in code:
            self._swap({'fingerprint': _FP_OLD})
            return 'armed'
        return json.dumps(self.state)


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
                     'rollback_identity', 'rollback_health', 'verify_reupdate',
                     'reupdate_identity', 'reupdate_health'):
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

    def test_dialog_guard_is_rearmed_around_every_swap(self):
        """A swap wipes storage; an unseeded modal would freeze the smoke."""
        self._run()
        self.assertGreaterEqual(len(self.td.seeds), 3)
        self.assertIn('Embody Update', self.td.seeds[1],
                      'the rollback needs the Restore Backup answer seeded')


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

    def test_rollback_that_restores_the_wrong_component_fails(self):
        """The swap happens (a new op id, the sentinel clears) but what came
        back is not the pre-update build."""
        real = self.td._swap

        def swap(to, **kw):
            if to.get('fingerprint') == _FP_OLD:
                to = dict(to, fingerprint='c' * 64)
            real(to, **kw)
        self.td._swap = swap
        res = self._run()
        self.assertFalse(res['ok'])
        self.assertIn('rollback_identity', res['error'])
        self.assertFalse(self._step(res, 'rollback_identity')['ok'])

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
