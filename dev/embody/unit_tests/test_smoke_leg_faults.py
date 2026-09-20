"""
Test suite: the FAULTS leg of the release smoke (smoke_legs/faults.py and
its probe kit smoke_legs/_probe.py).

Pure Python, TD-import-free: the leg only ever touches the run directory
and the `ctx` the orchestrator hands it, so a scripted fake TouchDesigner
(fake clock, fake ports, real files in a temp run dir) drives every path.
Inside TD every test skips -- the live half is exercised by the smoke.

What this pins, for each injected fault, is that the PROOF is load-bearing:
- a venv repair that never completes, or one that quietly loses
  site-packages entries, must FAIL the leg -- and the broken pyvenv.cfg
  must be put back;
- an Envoy that re-takes the occupied port must FAIL, not pass because it
  came back at all;
- a revive with no watchdog line in the log is not evidence the WATCHDOG
  did it (the auto-restart backoff would look identical from outside);
- a registry left invalid must FAIL and be restored from the copy;
- and run() reports through the result dict in every one of those cases,
  because the contract forbids raising out of a leg.
"""

import importlib
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

if not _IN_TD:
    if _RT not in sys.path:
        sys.path.insert(0, _RT)
    faults = importlib.import_module('smoke_legs.faults')
    probe = importlib.import_module('smoke_legs._probe')


# ===========================================================================
# the scripted fake TouchDesigner
# ===========================================================================

class _Sock:
    def __init__(self, td, port):
        self.td, self.port = td, port

    def close(self):
        self.td.occupied.discard(self.port)


class _FakeTD:
    """Everything the leg can observe about the smoke TD: a fake clock, a
    set of listening ports, the log file, and the file rewrites Embody
    performs on a restart. Each recovery is a switch a test can flip off to
    watch the matching proof fail."""

    BASE = 9870

    def __init__(self, run_dir, port=9871, pid=4242):
        self.run_dir = run_dir
        self.now = 0.0
        self.pid = pid
        self.last_port = port
        self.listening = {port}
        self.foreign = {self.BASE}   # another TD already holds the default
        self.occupied = set()        # ports the LEG holds
        self.enable = 1
        self.status = 'Running on port %d' % port
        self.queue = []
        self.repair_state = []
        self.state = {'port': port}
        self.seen = []
        self.project_folder = run_dir
        # recoveries under test
        self.repair = True
        self.mutate_site = False
        self.add_site = False
        self.move_port = True
        self.watchdog = True
        self.watchdog_logs = True
        self.fix_registry = True
        self.write_registry(port)
        self.write_mcp(self.venv_python)

    # --- clock -------------------------------------------------------
    def clock(self):
        return self.now

    def sleep(self, dt):
        self.now += max(float(dt), 0.001)
        while self.queue and self.queue[0][0] <= self.now:
            self.queue.pop(0)[1]()

    def at(self, delay, fn):
        self.queue.append((self.now + delay, fn))
        self.queue.sort(key=lambda row: row[0])

    # --- ports -------------------------------------------------------
    def alive(self, port):
        return port in self.listening or port in self.occupied

    def hold(self, port):
        if port in self.listening or port in self.foreign or \
                port in self.occupied:
            return None
        self.occupied.add(port)
        return _Sock(self, port)

    def _free_port(self):
        taken = self.listening | self.foreign | self.occupied
        for port in range(self.BASE, self.BASE + 10):
            if port not in taken:
                return port
        return None

    # --- files -------------------------------------------------------
    @property
    def venv_python(self):
        return self.run_dir.replace('\\', '/') + '/.venv/Scripts/python.exe'

    @property
    def cfg(self):
        return os.path.join(self.run_dir, '.venv', 'pyvenv.cfg')

    def write_registry(self, port):
        probe.write(
            os.path.join(self.run_dir, '.embody', 'envoy.json'),
            json.dumps({'active': 'smoke', 'td_executable': 'td.exe',
                        'instances': {'smoke': {'toe_path': 'smoke.toe',
                                                'port': port,
                                                'td_pid': self.pid}}}))

    def clear_registry(self):
        probe.write(os.path.join(self.run_dir, '.embody', 'envoy.json'),
                    json.dumps({'active': '', 'instances': {}}))

    def write_mcp(self, command):
        probe.write(os.path.join(self.run_dir, '.mcp.json'),
                    json.dumps({'mcpServers': {'envoy': {
                        'type': 'stdio', 'command': command, 'args': []}}}))

    def log(self, line):
        path = os.path.join(self.run_dir, 'logs', 'embody.log')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')

    # --- server lifecycle --------------------------------------------
    def stop(self, delay=2.0):
        def go():
            self.listening.clear()
            self.status = 'Disabled'
            self.clear_registry()
        self.at(delay, go)

    def start(self, delay=6.0):
        def go():
            port = self._free_port() if self.move_port else self.last_port
            self.last_port = port
            self.listening = {port}
            self.status = 'Running on port %d' % port
            if self.fix_registry:
                self.write_registry(port)
            if os.path.isdir(probe.home_of(probe.read(self.cfg))):
                self.write_mcp(self.venv_python)
            else:
                self.log('WARNING Venv Python at %s does not run (exit code '
                         '106); using system Python for the MCP bridge until '
                         'it is repaired' % self.venv_python)
                self.write_mcp('python')
                self.begin_repair()
        self.at(delay, go)

    def begin_repair(self, delay=8.0, good_home=None):
        if not self.repair:
            return
        home = good_home or os.path.join(self.run_dir, 'fake-td-bin')

        def go():
            text, _ = probe.break_home(probe.read(self.cfg), home)
            probe.write(self.cfg, text)
            site = probe.venv_paths(self.run_dir)[1]
            if self.mutate_site:
                names = probe.entries(site)
                os.rename(os.path.join(site, names[0]),
                          os.path.join(site, names[0] + '_moved'))
            if self.add_site:
                os.makedirs(os.path.join(site, 'uv_leftover'), exist_ok=True)
            self.repair_state = ['repaired']
            self.log('SUCCESS Venv interpreter repaired')
            self.write_mcp(self.venv_python)
        self.at(delay, go)

    def window(self):
        """Envoyenable off, then on again -- faults 2 and 4."""
        self.stop(2.0)
        self.at(2.0, lambda: setattr(self, 'enable', 0))
        self.start(18.0)
        self.at(18.0, lambda: setattr(self, 'enable', 1))

    def watchdog_kill(self):
        """Socket dies, Envoyenable untouched; only the watchdog comes back."""
        self.stop(2.0)
        if not self.watchdog:
            return
        if self.watchdog_logs:
            self.at(12.0, lambda: self.log(
                "WARNING Watchdog: enabled but socket dead (status 'Running "
                "on port %d') -- reviving" % self.last_port))
        self.start(14.0)

    # --- the ctx callables -------------------------------------------
    def py(self, code, timeout=30):
        self.seen.append(code)
        if '_venv_probe_ok' in code:
            return 'cleared'
        if '_embody_venv_repair_state' in code:
            return str(self.repair_state)
        if 'Envoystatus.eval()' in code:
            return self.status
        if 'Envoyenable.eval()' in code:
            return str(self.enable)
        if 'project.folder' in code:
            return self.project_folder
        if 'from td import run as _run' in code:
            if '_restart_count' in code:
                self.watchdog_kill()
            elif 'Envoyenable = False' in code:
                self.window()
            else:
                raise AssertionError('unrecognised deferred script')
            return 'scheduled'
        raise AssertionError('unscripted execute_python: %r' % code[:90])

    def call(self, name, arguments, timeout=30):
        if self.state['port'] not in self.listening:
            raise RuntimeError('%s: connection refused' % name)
        return {'version': '6.2.57', 'port': self.state['port']}


class _Case(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only')
        self.root = tempfile.mkdtemp(prefix='embody_smoke_faults_')
        self.home = os.path.join(self.root, 'fake-td-bin')
        for rel in ('.embody', 'logs', 'fake-td-bin',
                    os.path.join('.venv', 'Scripts'),
                    os.path.join('.venv', 'Lib', 'site-packages')):
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)
        site = os.path.join(self.root, '.venv', 'Lib', 'site-packages')
        for name in ('mcp', 'fastmcp', 'pydantic', 'uvicorn'):
            os.makedirs(os.path.join(site, name), exist_ok=True)
        probe.write(os.path.join(self.root, '.venv', 'Scripts', 'python.exe'),
                    'not really an exe')
        probe.write(os.path.join(self.root, '.venv', 'pyvenv.cfg'),
                    'home = %s\nimplementation = CPython\n'
                    'version_info = 3.11.15\n' % self.home)
        probe.write(os.path.join(self.root, 'logs', 'embody.log'), '')
        self.td = _FakeTD(self.root)
        self.ctx = self._ctx(self.td)
        self.rec = probe.Rec(self.ctx)
        self.sm = probe.seams(self.ctx)
        self.st = {'port': self.ctx['port']}

    def _ctx(self, td):
        return {'run_dir': self.root, 'repo': '', 'build': {},
                'installed': {}, 'upgrade_from': None,
                'port': td.state['port'], 'pid': td.pid,
                'td_exe': 'td.exe', 'platform': 'win32',
                'set_port': lambda p: td.state.__setitem__('port', int(p)),
                'call': td.call, 'py': td.py, 'log': lambda m: None,
                'budget': lambda: 100000.0 - td.now,
                'wait_for_flag': lambda name, timeout, done: ('', False),
                '_seams': {'clock': td.clock, 'sleep': td.sleep,
                           'alive': td.alive, 'hold': td.hold}}

    def tearDown(self):
        if not _IN_TD and os.path.isdir(getattr(self, 'root', '')):
            shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    # helpers
    def step(self, name):
        for s in self.rec.steps:
            if s['step'] == name:
                return s
        return None

    def assertStepOk(self, name):
        got = self.step(name)
        self.assertIsNotNone(got, '%s never ran: %s'
                             % (name, [s['step'] for s in self.rec.steps]))
        self.assertTrue(got['ok'], '%s failed: %s' % (name, got['detail']))

    def assertStepFailed(self, name):
        got = self.step(name)
        self.assertIsNotNone(got, '%s never ran: %s'
                             % (name, [s['step'] for s in self.rec.steps]))
        self.assertFalse(got['ok'], '%s unexpectedly passed' % name)


# ===========================================================================
# the deferred scripts -- what the leg asks TD to do
# ===========================================================================

class TestInjectionScripts(_Case):

    def test_every_kill_is_deferred_never_inline(self):
        """The server being stopped is the one answering the call, so the
        stop has to be handed to TD's own run() and come back first."""
        probe.defer(self.ctx, faults._window(15000))
        sent = self.td.seen[-1]
        self.assertIn('from td import run as _run', sent)
        self.assertIn('delayMilliSeconds=1500', sent)
        self.assertNotIn('\nop.Embody.par.Envoyenable = False', sent)

    def test_watchdog_kill_never_touches_the_enable_flag(self):
        """Fault 3 is only the watchdog's case while Envoyenable stays on."""
        script = faults._watchdog_kill(9871)
        self.assertNotIn('Envoyenable', script)
        self.assertIn("'Running on port 9871'", script)

    def test_watchdog_kill_rearms_the_backoff_after_stop_not_before(self):
        """Stop() zeroes the restart window at its top, so an earlier
        assignment would be wiped and the ~1s auto-restart, not the
        watchdog, would be the thing that recovered."""
        script = faults._watchdog_kill(9871)
        self.assertLess(script.index('e.Stop()'),
                        script.index('_restart_count'))
        self.assertIn('_last_start_time', script)
        self.assertIn('_restart_window_start', script)

    def test_watchdog_kill_carries_its_own_safety_net(self):
        script = faults._watchdog_kill(9871)
        self.assertIn('_probeAlive', script)
        self.assertIn('delayMilliSeconds=200000', script)

    def test_window_turns_the_master_switch_off_then_back_on(self):
        script = faults._window(15000)
        self.assertIn('op.Embody.par.Envoyenable = False', script)
        self.assertEqual(script.count('Envoyenable = True'), 2)
        self.assertIn('delayMilliSeconds=15000', script)


# ===========================================================================
# 1. broken venv interpreter
# ===========================================================================

class TestVenvFault(_Case):

    def test_fallback_then_in_place_repair(self):
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        for name in ('venv.inject', 'venv.restart', 'venv.fallback',
                     'venv.repair', 'venv.mcp_json', 'venv.no_deletion',
                     'venv.pyvenv_cfg', 'venv.running'):
            self.assertStepOk(name)
        self.assertTrue(probe.is_venv_command(probe.mcp_command(self.root),
                                              self.root))
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)

    def test_the_system_python_fallback_is_really_observed(self):
        """The .mcp.json command passes through system Python before the
        repair puts it back -- if it never did, the fallback did not fire."""
        seen = []
        real = self.td.write_mcp
        self.td.write_mcp = lambda cmd: (seen.append(cmd), real(cmd))[1]
        faults._fault_venv(self.ctx, self.rec, self.sm, self.st)
        self.assertIn('python', seen)
        self.assertTrue(probe.is_venv_command(seen[-1], self.root))

    def test_repair_that_never_completes_fails_and_restores_the_cfg(self):
        self.td.repair = False
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepOk('venv.fallback')
        self.assertStepFailed('venv.repair')
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home,
                         'a failed step must put pyvenv.cfg back')

    def test_a_repair_that_loses_site_packages_fails(self):
        """Nothing may be deleted: the server is running from these."""
        self.td.mutate_site = True
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepOk('venv.repair')
        self.assertStepFailed('venv.no_deletion')

    def test_a_repair_state_that_never_says_repaired_fails(self):
        self.td.repair_state = ['failed']
        self.td.begin_repair = lambda *a, **k: self.td.at(8.0, lambda: (
            self.td.log('SUCCESS Venv interpreter repaired'),
            self.td.write_mcp(self.td.venv_python)))
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.repair')

    def test_an_added_site_packages_entry_is_reported_not_failed(self):
        """The invariant is that nothing was DELETED; uv touching the venv
        must not read as a loss."""
        self.td.add_site = True
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertStepOk('venv.no_deletion')
        self.assertIn('uv_leftover', self.step('venv.no_deletion')['detail'])

    def test_home_written_with_another_spelling_still_matches(self):
        """uv may rewrite pyvenv.cfg's home with different separators."""
        self.assertTrue(faults._same_path(self.home.replace(os.sep, '/'),
                                          self.home))
        self.assertTrue(faults._same_path(self.home.upper(), self.home))
        self.assertFalse(faults._same_path(self.home + '-gone', self.home))
        self.assertFalse(faults._same_path('', self.home))

    def test_a_missing_pyvenv_cfg_is_a_clean_failure(self):
        os.rename(self.td.cfg, self.td.cfg + '.away')
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.inject')


# ===========================================================================
# 2. the port Envoy wants is occupied
# ===========================================================================

class TestPortFault(_Case):

    def test_envoy_moves_off_the_occupied_port(self):
        old = self.st['port']
        self.assertTrue(faults._fault_port(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertStepOk('port.hold')
        self.assertStepOk('port.fallback')
        self.assertStepOk('port.running')
        self.assertNotEqual(self.st['port'], old)
        self.assertEqual(self.ctx['run_dir'], self.root)

    def test_a_port_that_did_not_change_fails(self):
        self.td.move_port = False
        self.assertFalse(faults._fault_port(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('port.fallback')

    def test_the_occupier_is_released_even_when_the_step_fails(self):
        self.td.move_port = False
        faults._fault_port(self.ctx, self.rec, self.sm, self.st)
        self.assertEqual(self.td.occupied, set(),
                         'the leg must not keep holding the port')

    def test_a_port_that_never_frees_fails_before_binding(self):
        self.td.stop = lambda delay=2.0: None      # the kill never lands
        self.td.start = lambda delay=6.0: None
        self.assertFalse(faults._fault_port(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('port.hold')


# ===========================================================================
# 3. dead socket, Envoyenable still on
# ===========================================================================

class TestWatchdogFault(_Case):

    def test_watchdog_revives_the_dead_socket(self):
        self.assertTrue(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                               self.st))
        self.assertStepOk('watchdog.kill')
        self.assertStepOk('watchdog.revive')
        self.assertStepOk('watchdog.enable_flag')
        self.assertStepOk('watchdog.running')

    def test_a_watchdog_that_never_revives_fails(self):
        self.td.watchdog = False
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.revive')

    def test_a_revive_with_no_watchdog_line_is_not_proof(self):
        """Coming back is not evidence the WATCHDOG did it -- the restart
        backoff looks identical from outside the process."""
        self.td.watchdog_logs = False
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.revive')

    def test_an_enable_flag_that_went_off_fails(self):
        """A revive that only came back because the master switch was
        toggled is a different recovery, and must not read as the watchdog's.
        """
        self.td.enable = 0
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.enable_flag')


# ===========================================================================
# 4. corrupt instance registry
# ===========================================================================

class TestRegistryFault(_Case):

    def test_corrupt_registry_is_rewritten_valid(self):
        self.assertTrue(faults._fault_registry(self.ctx, self.rec, self.sm,
                                               self.st))
        self.assertStepOk('registry.inject')
        self.assertStepOk('registry.rewrite')
        data = probe.registry(self.root)
        self.assertIsInstance(data, dict)
        self.assertIn('instances', data)
        self.assertEqual(probe.reg_ports(self.root), [self.st['port']])

    def test_a_registry_that_stays_invalid_fails_and_is_restored(self):
        self.td.fix_registry = False
        self.assertFalse(faults._fault_registry(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('registry.rewrite')
        self.assertIsNotNone(probe.registry(self.root),
                             'the copy kept aside must be put back')

    def test_a_rewrite_under_another_pid_fails(self):
        self.td.pid = 9999            # the rewrite claims a different TD
        self.assertFalse(faults._fault_registry(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('registry.rewrite')

    def test_the_garbage_really_is_unparseable(self):
        probe.write(os.path.join(self.root, '.embody', 'envoy.json'),
                    faults._GARBAGE)
        self.assertIsNone(probe.registry(self.root))
        self.assertEqual(probe.reg_ports(self.root), [])


# ===========================================================================
# the leg as a whole
# ===========================================================================

class TestLeg(_Case):

    def test_a_port_answering_for_another_project_injects_nothing(self):
        """A collided or stale port must never carry a fault into someone
        else's TouchDesigner."""
        self.td.project_folder = 'C:/Users/dev/Documents/Embody/dev'
        got = faults.run(self.ctx)
        self.assertFalse(got['ok'])
        self.assertEqual([s['step'] for s in got['steps']], ['leg.target'])
        self.assertIn('nothing was injected', got['error'])
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)

    def test_all_four_faults_pass_and_leave_envoy_up(self):
        got = faults.run(self.ctx)
        self.assertTrue(got['ok'], got)
        self.assertEqual(got['error'], '')
        prefixes = {s['step'].split('.')[0] for s in got['steps']}
        self.assertEqual(prefixes,
                         {'leg', 'venv', 'port', 'watchdog', 'registry'})
        self.assertEqual(got['steps'][0]['step'], 'leg.target')
        self.assertTrue(self.td.listening)
        self.assertIn(self.td.state['port'], self.td.listening)

    def test_one_failed_fault_stops_the_rest(self):
        self.td.repair = False
        got = faults.run(self.ctx)
        self.assertFalse(got['ok'])
        self.assertEqual({s['step'].split('.')[0] for s in got['steps']}
                         - {'leg'}, {'venv'})

    def test_run_reports_instead_of_raising(self):
        def boom(code, timeout=30):
            raise RuntimeError('bridge is gone')
        self.ctx['py'] = boom
        got = faults.run(self.ctx)
        self.assertFalse(got['ok'])
        self.assertIn('bridge is gone', got['error'])
        self.assertIsInstance(got['steps'], list)

    def test_leaving_without_a_reachable_envoy_is_recorded(self):
        self.td.watchdog = False
        self.td.repair_state = ['repaired']
        got = faults.run(self.ctx)
        self.assertFalse(got['ok'])
        self.assertIn('faults_backup', os.listdir(self.root))

    def test_the_result_shape_matches_the_leg_contract(self):
        got = faults.run(self.ctx)
        self.assertEqual(sorted(got), ['error', 'ok', 'steps'])
        for step in got['steps']:
            self.assertEqual(sorted(step), ['detail', 'ok', 'step'])


# ===========================================================================
# the probe kit
# ===========================================================================

class TestProbeKit(_Case):

    def test_break_home_only_touches_the_home_line(self):
        text = 'home = C:/TD/bin\nversion_info = 3.11.15\nuv = 0.12\n'
        out, hit = probe.break_home(text, 'C:/nope')
        self.assertTrue(hit)
        self.assertEqual(probe.home_of(out), 'C:/nope')
        self.assertIn('version_info = 3.11.15', out)
        self.assertIn('uv = 0.12', out)

    def test_break_home_reports_a_cfg_with_no_home(self):
        out, hit = probe.break_home('version_info = 3.11.15\n', 'x')
        self.assertFalse(hit)
        self.assertEqual(out, 'version_info = 3.11.15\n')

    def test_is_venv_command_rejects_system_python(self):
        self.assertFalse(probe.is_venv_command('python', self.root))
        self.assertFalse(probe.is_venv_command(
            'C:/elsewhere/.venv/Scripts/python.exe', self.root))
        self.assertTrue(probe.is_venv_command(
            self.root + '\\.venv\\Scripts\\python.exe', self.root))

    def test_reads_never_raise_on_a_missing_or_broken_file(self):
        empty = tempfile.mkdtemp(prefix='embody_smoke_faults_empty_')
        try:
            self.assertIsNone(probe.registry(empty))
            self.assertEqual(probe.reg_ports(empty), [])
            self.assertEqual(probe.mcp_command(empty), '')
            self.assertEqual(probe.logs(empty), '')
            self.assertEqual(probe.entries(os.path.join(empty, 'nope')), [])
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_venv_paths_finds_the_posix_layout(self):
        posix = tempfile.mkdtemp(prefix='embody_smoke_faults_posix_')
        try:
            site = os.path.join(posix, '.venv', 'lib', 'python3.11',
                                'site-packages')
            os.makedirs(site)
            self.assertEqual(probe.venv_paths(posix)[1], site)
        finally:
            shutil.rmtree(posix, ignore_errors=True)

    def test_until_stops_on_the_runs_own_ceiling(self):
        """A leg that outran budget() would take teardown down with it."""
        self.ctx['budget'] = lambda: 10.0      # below RESERVE_S
        self.assertIsNone(probe.until(self.ctx, self.sm, lambda: False, 60.0))

    def test_until_checks_before_it_sleeps(self):
        before = self.td.now
        self.assertTrue(probe.until(self.ctx, self.sm, lambda: True, 60.0))
        self.assertEqual(self.td.now, before)

    def test_live_port_skips_a_registered_port_that_is_dead(self):
        self.td.listening.clear()
        self.assertIsNone(probe.live_port(self.ctx, self.sm))
        self.td.listening.add(self.st['port'])
        self.assertEqual(probe.live_port(self.ctx, self.sm), self.st['port'])
        self.assertIsNone(probe.live_port(self.ctx, self.sm,
                                          avoid=(self.st['port'],)))

    def test_settle_points_the_ctx_at_the_new_port(self):
        self.td.listening = {9875}
        self.td.write_registry(9875)
        self.assertEqual(probe.settle(self.ctx, self.sm, self.st, 30.0), 9875)
        self.assertEqual(self.st['port'], 9875)
        self.assertEqual(self.td.state['port'], 9875)

    def test_backup_keeps_a_copy_under_the_run_dir(self):
        path = os.path.join(self.root, '.mcp.json')
        kept = probe.backup(self.root, path)
        self.assertTrue(kept.startswith(os.path.join(self.root, probe.BACKUP)))
        self.assertEqual(probe.read(kept), probe.read(path))

    def test_rec_records_every_step_in_order(self):
        rec = probe.Rec(self.ctx)
        rec.ok('a', 'fine')
        rec.check('b', 0, 'nope')
        self.assertEqual([(s['step'], s['ok']) for s in rec.steps],
                         [('a', True), ('b', False)])
