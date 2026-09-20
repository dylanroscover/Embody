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
import re
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

    def __init__(self, run_dir, port=9871, pid=4242, platform='win32'):
        self.run_dir = run_dir
        self.platform = platform
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
        self.gen = 0                 # COMP storage '_smoke_gen'
        self.running_flag = True     # COMP storage 'envoy_running'
        self.attrs = True            # the private names fault 3 writes exist
        # envoy_setup caches a successful probe per venv path: a restart that
        # finds the cache warm never probes, so the leg's clear is required.
        self.probe_cache = self.venv_python
        # recoveries under test
        self.repair = True
        self.probe_verdict = 'broken'    # 'timeout' falls back without repair
        self.fallback_logs = True
        self.fix_mcp = True
        self.venv_still_runs = False     # nothing the leg does breaks it
        self.repair_interpreter = True   # uv rewrites the bin/ launchers
        self.mutate_site = False
        self.add_site = False
        self.move_port = True
        self.watchdog = True
        self.watchdog_logs = True
        self.watchdog_revive_logs = True
        self.watchdog_branch = 'socket dead'
        self.clobber_wedge_at = None
        self.wedge_reasserts = (3.0, 5.0, 7.0, 9.0)
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

    def runs_probe(self, python_path, timeout=10.0):
        return self.venv_still_runs or self._runs_now()

    def _runs_now(self):
        """What probe_venv_python would answer. On posix the launcher is a
        symlink into TD's framework and ignores pyvenv.cfg's home (CPython
        getpath.py:359 prefers realpath(executable)); the win32 trampoline
        reads home and dies on a bogus one."""
        if not self._interpreter_intact():
            return False
        if self.platform != 'win32':
            return True
        return os.path.isdir(probe.home_of(probe.read(self.cfg)))

    def _interpreter_intact(self):
        try:
            return 'faults leg' not in probe.read(self.interpreter)
        except OSError:
            return False

    def _free_port(self):
        taken = self.listening | self.foreign | self.occupied
        for port in range(self.BASE, self.BASE + 10):
            if port not in taken:
                return port
        return None

    # --- files -------------------------------------------------------
    @property
    def venv_python(self):
        return probe.venv_python(self.run_dir,
                                 self.platform).replace('\\', '/')

    @property
    def interpreter(self):
        """The file behind venv_python -- what uv rewrites and what the
        posix rung of the injection displaces."""
        return probe.venv_interpreter(self.run_dir, self.platform)

    def restore_interpreter(self):
        """uv venv --allow-existing writes the bin/ launchers over whatever
        is there (uv-fs replace_symlink: temp symlink, then rename)."""
        probe.write(self.interpreter, 'a working interpreter')

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
            self.running_flag = False
            self.status = 'Disabled'
            self.clear_registry()
        self.at(delay, go)

    def _fallback_line(self):
        """envoy_setup's two probe branches, spelled as it writes them."""
        if self.probe_verdict == 'broken':
            return ('WARNING Venv Python at %s does not run (exit code 106); '
                    'using system Python for the MCP bridge until it is '
                    'repaired' % self.venv_python)
        return ('WARNING Venv Python probe timeout (no answer within 5s); '
                'using system Python for now (will re-probe on next start)')

    def _start_now(self):
        port = self._free_port() if self.move_port else self.last_port
        self.last_port = port
        self.listening = {port}
        self.running_flag = True
        self.status = 'Running on port %d' % port
        if self.fix_registry:
            self.write_registry(port)
        if self.probe_cache == self.venv_python:
            self.write_mcp(self.venv_python)     # cached: never probed
        elif self._runs_now():
            self.probe_cache = self.venv_python
            self.write_mcp(self.venv_python)
        else:
            if self.fallback_logs:
                self.log(self._fallback_line())
            self.write_mcp('python')
            if self.probe_verdict == 'broken':
                self.begin_repair()

    def start(self, delay=6.0):
        self.at(delay, self._start_now)

    def begin_repair(self, delay=8.0, good_home=None):
        if not self.repair:
            return
        home = good_home or os.path.join(self.run_dir, 'fake-td-bin')

        def go():
            text, _ = probe.break_home(probe.read(self.cfg), home)
            probe.write(self.cfg, text)
            if self.repair_interpreter:
                self.restore_interpreter()
            site = probe.venv_paths(self.run_dir)[1]
            if self.mutate_site:
                names = probe.entries(site)
                os.rename(os.path.join(site, names[0]),
                          os.path.join(site, names[0] + '_moved'))
            if self.add_site:
                os.makedirs(os.path.join(site, 'uv_leftover'), exist_ok=True)
            self.repair_state = ['repaired']
            self.log('SUCCESS Venv interpreter repaired')
            if self.fix_mcp:
                self.write_mcp(self.venv_python)
        self.at(delay, go)

    def window(self, gen, up=18.0):
        """Envoyenable off, then the leg's generation-guarded re-enable --
        faults 1, 2 and 4. A timer armed under an older generation no-ops."""
        self.stop(2.0)
        self.at(2.0, lambda: setattr(self, 'enable', 0))

        def back():
            if self.gen != gen:
                return
            self.enable = 1
            self._start_now()
        self.at(up, back)

    def _wedge(self):
        """What the kill script writes right after Stop() cleared it."""
        self.running_flag = True
        self.status = 'Running on port %d' % self.last_port

    def watchdog_kill(self):
        """Socket dies, Envoyenable untouched; only the watchdog comes back.

        `clobber_wedge_at` models the exit hook landing after the kill
        script (macOS CI 2026-09-20): envoy_running goes False and the
        status reads 'Disabled', which the watchdog refuses to revive. The
        leg's re-assertions have to put the wedge back before its tick."""
        self.stop(2.0)
        self.at(2.0, self._wedge)
        if self.clobber_wedge_at is not None:
            self.at(self.clobber_wedge_at, self._clobber_wedge)
        for at in self.wedge_reasserts:
            self.at(at, self._reassert_wedge)
        if not self.watchdog:
            return
        # The watchdog only revives an enabled server whose status has not
        # been turned off under it.
        self.at(12.0, self._watchdog_tick)

    def _clobber_wedge(self):
        self.running_flag = False
        self.status = 'Disabled'

    def _reassert_wedge(self):
        """What each scheduled re-assertion does. It stands down once the
        socket answers again -- without that it overwrites the status the
        watchdog just corrected, with the port it killed."""
        if self.listening:
            return
        if not str(self.status).startswith('Reviving'):
            self._wedge()

    def _watchdog_tick(self):
        if not self.running_flag or str(self.status) == 'Disabled':
            return          # a disabled Envoy is never revived
        if self.watchdog_logs:
            self._watchdog_logs()
        self.status = 'Reviving (watchdog)...'
        self.start(2.0)

    def _watchdog_logs(self):
        """Both lines the product writes: the branch that noticed, then
        _reviveDeadServer's own (EnvoyExt.py:6687). Only the second is
        proof -- nothing but _watchdogTick reaches it."""
        if self.watchdog_branch == 'socket dead':
            self.log("WARNING Watchdog: enabled but socket dead (status "
                     "'Running on port %d') -- reviving" % self.last_port)
        else:
            self.log("WARNING Watchdog: status stuck at 'Restarting "
                     "(attempt 8)...' ~24s while enabled -- forcing restart")
        if self.watchdog_revive_logs:
            self.log("WARNING Watchdog: MCP socket on port %d unreachable "
                     "while enabled (running=True) -- reviving server"
                     % self.last_port)

    # --- the ctx callables -------------------------------------------
    @staticmethod
    def _gen_of(code):
        # the script may arrive escaped inside defer's repr
        hit = re.search(r"_smoke_gen\\?', (\d+)", code)
        return int(hit.group(1)) if hit else None

    def py(self, code, timeout=30):
        self.seen.append(code)
        if 'from td import run as _run' in code:
            gen = self._gen_of(code)
            if gen is not None:
                self.gen = gen
            if '_restart_count' in code:
                self.watchdog_kill()
            elif 'Envoyenable = False' in code:
                self.window(gen)
            else:
                raise AssertionError('unrecognised deferred script')
            return 'scheduled'
        if '_venv_probe_ok' in code:
            self.probe_cache = ''
            self.repair_state = []
            return 'cleared'
        if '_embody_venv_repair_state' in code:
            return str(self.repair_state)
        if 'hasattr' in code:
            if not self.attrs:
                return "no attribute ['_restart_count']"
            return 'ready' if self.running_flag else 'envoy_running is False'
        if "store('_smoke_gen'" in code:
            self.gen = self._gen_of(code)
            return 'stored'
        if 'Envoystatus.eval()' in code:
            return self.status
        if 'Envoyenable.eval()' in code:
            return str(self.enable)
        if 'project.folder' in code:
            return self.project_folder
        raise AssertionError('unscripted execute_python: %r' % code[:90])

    def call(self, name, arguments, timeout=30):
        if self.state['port'] not in self.listening:
            raise RuntimeError('%s: connection refused' % name)
        return {'version': '6.2.57', 'port': self.state['port']}


class _Case(EmbodyTestCase):

    PLATFORM = 'win32'      # a subclass flips it; nothing here is skipped

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only')
        self.root = tempfile.mkdtemp(prefix='embody_smoke_faults_')
        self.home = os.path.join(self.root, 'fake-td-bin')
        rels = ['.embody', 'logs', 'fake-td-bin']
        rels += ([os.path.join('.venv', 'Scripts'),
                  os.path.join('.venv', 'Lib', 'site-packages')]
                 if self.PLATFORM == 'win32' else
                 [os.path.join('.venv', 'bin'),
                  os.path.join('.venv', 'lib', 'python3.11',
                               'site-packages')])
        for rel in rels:
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)
        site = probe.venv_paths(self.root)[1]
        for name in ('mcp', 'fastmcp', 'pydantic', 'uvicorn'):
            os.makedirs(os.path.join(site, name), exist_ok=True)
        self._make_interpreter()
        probe.write(os.path.join(self.root, '.venv', 'pyvenv.cfg'),
                    'home = %s\nimplementation = CPython\n'
                    'version_info = 3.11.15\n' % self.home)
        probe.write(os.path.join(self.root, 'logs', 'embody.log'), '')
        self.td = _FakeTD(self.root, platform=self.PLATFORM)
        self.ctx = self._ctx(self.td)
        self.rec = probe.Rec(self.ctx)
        self.sm = probe.seams(self.ctx)
        self.st = {'port': self.ctx['port'], 'gen': 0, 'restore': [],
                   'displaced': []}

    def _make_interpreter(self):
        """The venv's launcher directory. uv writes SYMLINKS on posix
        (bin/python3 -> python -> TD's framework binary, uv 0.9.27
        virtualenv.rs:259-271); a box that refuses symlinks gets plain
        files, which resolve to one file and exercise the same displace."""
        path = probe.venv_python(self.root, self.PLATFORM)
        probe.write(path if self.PLATFORM == 'win32'
                    else os.path.join(os.path.dirname(path), 'python'),
                    'a working interpreter')
        if self.PLATFORM == 'win32':
            return
        try:
            os.symlink('python', path)
        except OSError:
            probe.write(path, 'a working interpreter')

    def _home_ok(self, healed):
        """The venv.pyvenv_cfg gate, spelled as _fault_venv spells it."""
        return os.path.isdir(healed) and probe.same_path(healed, self.home)

    def _ctx(self, td):
        return {'run_dir': self.root, 'repo': '', 'build': {},
                'installed': {}, 'upgrade_from': None,
                'port': td.state['port'], 'pid': td.pid,
                'td_exe': 'td.exe', 'platform': self.PLATFORM,
                'set_port': lambda p: td.state.__setitem__('port', int(p)),
                'call': td.call, 'py': td.py, 'log': lambda m: None,
                'budget': lambda: 100000.0 - td.now,
                'wait_for_flag': lambda name, timeout, done: ('', False),
                '_seams': {'clock': td.clock, 'sleep': td.sleep,
                           'alive': td.alive, 'hold': td.hold,
                           'runs': td.runs_probe}}

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
        probe.defer(self.ctx, faults._window(15000, 1))
        sent = self.td.seen[-1]
        self.assertIn('from td import run as _run', sent)
        self.assertIn('delayMilliSeconds=1500', sent)
        self.assertNotIn('\nop.Embody.par.Envoyenable = False', sent)

    def test_every_deferred_delay_is_wall_time(self):
        """The leg's deadlines are time.monotonic; a frame-clock delay
        would drift against every one of them."""
        probe.defer(self.ctx, faults._window(15000, 1))
        self.assertIn('wallTime=True', self.td.seen[-1])
        self.assertEqual(faults._window(15000, 1).count('delayMilliSeconds'),
                         faults._window(15000, 1).count('wallTime=True'))

    def test_watchdog_kill_never_touches_the_enable_flag(self):
        """Fault 3 is only the watchdog's case while Envoyenable stays on."""
        script = faults._watchdog_kill(9871, 3)
        self.assertNotIn('Envoyenable', script)
        self.assertIn("'Running on port 9871'", script)

    def test_watchdog_kill_rearms_the_backoff_after_stop_not_before(self):
        """Stop() zeroes the restart window at its top, so an earlier
        assignment would be wiped and the ~1s auto-restart, not the
        watchdog, would be the thing that recovered."""
        script = faults._watchdog_kill(9871, 3)
        self.assertLess(script.index('e.Stop()'),
                        script.index('_restart_count'))
        self.assertIn('_last_start_time', script)
        self.assertIn('_restart_window_start', script)

    def test_watchdog_kill_re_asserts_the_wedge_until_it_takes(self):
        """One assertion is not enough: it races the server thread's exit
        hook, which clears envoy_running and can leave the status
        'Disabled' (EnvoyExt.Stop:6712) -- and a disabled Envoy is never
        revived. Every re-assertion is generation-guarded and stands down
        once the revive has started."""
        script = faults._watchdog_kill(9871, 3, 'C:/run')
        self.assertEqual(script.count('delayMilliSeconds'),
                         len(faults._WEDGE_AT_S))
        self.assertEqual(
            script.count("if g == 3 and not back and not st.startswith("
                         "'Reviving')"), len(faults._WEDGE_AT_S))
        self.assertIn("o.store('envoy_running', True)", script)
        self.assertIn('faults_wedge_3.json', script)
        # The backoff is re-armed AFTER Stop(), which zeroes the window at
        # its top -- otherwise the auto-restart, not the watchdog, recovers.
        self.assertLess(script.index('e.Stop()'),
                        script.index('e._restart_count = 7'))

    def test_window_arms_the_re_enable_on_two_independent_clocks(self):
        """One dispatch is a single point of failure: live 2026-09-20 the
        stop landed and the re-enable never did, and with the socket gone
        there was no MCP call left to rescue it. Both are guarded by the
        generation they were armed under, and both skip a par already on,
        so whichever fires second is a no-op."""
        script = faults._window(15000, 4, 'C:/run')
        self.assertIn('o.par.Envoyenable = False', script)
        self.assertEqual(script.count('delayMilliSeconds=15000'), 1)
        self.assertEqual(script.count('delayFrames=900'), 1)
        self.assertEqual(script.count('Envoyenable = True'), 2)
        self.assertEqual(script.count('if g == 4 and not was:'), 2)
        self.assertIn("store('_smoke_gen', 4)", script)
        for part in ("'by': 'clock'", "'by': 'frames'"):
            self.assertIn(part, script)

    def test_every_armed_script_compiles(self):
        for script in (faults._window(15000, 4, 'C:/run'),
                       faults._watchdog_kill(9871, 5)):
            compile(script, '<armed>', 'exec')

    def test_a_window_that_never_reopened_names_the_reason(self):
        """No breadcrumb means TD dispatched neither callback -- the
        failure that first read as a product bug."""
        self.assertIn('neither re-enable dispatched',
                      faults._why_shut(self.root, 9))
        probe.write(os.path.join(self.root, faults._WINDOW_CRUMB % 9),
                    '{"by": "clock", "gen": 8, "was": 1}' + chr(10))
        why = faults._why_shut(self.root, 9)
        self.assertIn('clock saw gen=8', why)
        self.assertIn('enable=1', why)

    def test_a_timer_from_an_earlier_fault_cannot_re_enable_envoy(self):
        """The frame-exact failure this guard exists for: fault 1's timer
        drifted into fault 4 and restarted Envoy there."""
        probe.defer(self.ctx, faults._window(15000, faults._bump(self.st)))
        self.td.sleep(3.0)                     # the switch is off
        self.assertEqual(self.td.enable, 0)
        self.td.gen = 99                       # a later fault moved on
        self.td.sleep(60.0)
        self.assertEqual(self.td.enable, 0, 'a stale timer re-enabled Envoy')
        self.assertEqual(self.td.listening, set())

    def test_the_cache_clear_never_rebinds_the_repair_dicts(self):
        """_pollVenvRepair's worker closed over them; a new dict strands it
        rescheduling forever (EnvoyExt.py:6063-6081)."""
        self.assertIn('.clear()', faults._CLEAR_CACHES)
        self.assertNotIn('= {}', faults._CLEAR_CACHES)


# ===========================================================================
# 1. broken venv interpreter
# ===========================================================================

class TestVenvFault(_Case):

    def test_fallback_then_in_place_repair(self):
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        for name in ('venv.inject', 'venv.restart', 'venv.fallback',
                     'venv.repair', 'venv.runs_again', 'venv.mcp_json',
                     'venv.no_deletion', 'venv.pyvenv_cfg', 'venv.running'):
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

    def test_a_late_failure_restores_both_corrupted_files(self):
        """The last checks used to return without restoring anything, so a
        red run left the venv broken and .mcp.json on system Python."""
        self.td.fix_mcp = False
        before = probe.read(os.path.join(self.root, '.mcp.json'))
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepOk('venv.repair')
        self.assertStepFailed('venv.mcp_json')
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)
        self.assertEqual(probe.read(os.path.join(self.root, '.mcp.json')),
                         before, '.mcp.json is backed up, so it is restored')

    def test_a_repair_that_writes_the_wrong_home_fails(self):
        """The venv must end up delegating to the interpreter it had."""
        other = os.path.join(self.root, 'other-td-bin')
        os.makedirs(other, exist_ok=True)
        real = self.td.begin_repair
        self.td.begin_repair = lambda *a, **k: real(good_home=other)
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.pyvenv_cfg')
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)

    def test_a_restart_that_logs_no_fallback_fails(self):
        self.td.fallback_logs = False
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.fallback')

    def test_a_warm_probe_cache_would_hide_the_fault(self):
        """The leg has to clear _venv_probe_ok: a cached OK means the
        restart never probes the interpreter it just broke."""
        real = self.td.py

        def no_clear(code, timeout=30):
            if '_venv_probe_ok' in code:
                return 'cleared'          # the clear is dropped
            return real(code, timeout)
        self.ctx['py'] = no_clear
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.fallback')

    def test_an_interpreter_that_still_starts_skips_the_fault(self):
        """macOS may keep running a venv python whose home is bogus. The
        leg proves the injection locally and skips instead of reding."""
        self.td.venv_still_runs = True
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertStepOk('venv.skipped')
        self.assertIsNone(self.step('venv.inject'))
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)

    def test_an_inconclusive_probe_skips_instead_of_failing(self):
        """A timed-out probe falls back to system Python and deliberately
        does not repair -- that is not a missing fallback."""
        self.td.probe_verdict = 'timeout'
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertStepOk('venv.skipped')
        self.assertIsNone(self.step('venv.repair'))
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)

    def test_site_packages_that_cannot_be_found_is_a_failure(self):
        """entries() of a missing directory is [], which would make the
        no-deletion proof compare [] with [] and pass on nothing."""
        site = probe.venv_paths(self.root)[1]
        os.rename(site, site + '.away')
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.inject')

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
        self.assertTrue(self._home_ok(self.home.replace(os.sep, '/')))
        self.assertFalse(self._home_ok(self.home + '-gone'))
        self.assertFalse(self._home_ok(''))

    def test_case_follows_the_filesystem_not_the_platform(self):
        """Windows and a default APFS volume accept either case; a
        case-sensitive volume does not. Asserting the Windows answer
        everywhere reddened macos-latest (CI 2026-09-20), so ask the
        filesystem the same question the code does."""
        other = self.home.upper()
        if other == self.home:
            self.skipTest('temp path has no case to flip')
        insensitive = os.path.isdir(other)
        self.assertEqual(self._home_ok(other), insensitive)

    def test_a_venv_that_was_already_dead_is_a_clean_failure(self):
        """Nothing below proves a recovery on an interpreter that never
        started: every rung would look like it took, and the honest skip
        would read as a platform verdict instead of a broken project."""
        probe.write(self.td.interpreter, probe.STUB)
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.inject')
        self.assertIn('before anything was injected',
                      self.step('venv.inject')['detail'])

    def test_a_missing_pyvenv_cfg_is_a_clean_failure(self):
        os.rename(self.td.cfg, self.td.cfg + '.away')
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.inject')


class TestVenvFaultOnDarwin(TestVenvFault):
    """Every proof above, re-run against the macOS model: a uv venv's
    bin/python3 is a symlink into TD's framework, so pyvenv.cfg's home is
    inert there and the leg escalates to displacing the interpreter. The
    macOS run of 2026-09-20 recorded venv.skipped and proved none of this,
    so it is MODELLED here rather than skipped on a Windows box."""

    PLATFORM = 'darwin'

    def test_the_posix_rung_displaces_the_interpreter_and_spares_the_cfg(self):
        """pyvenv.cfg has to be back before the escalation: repair_venv_
        interpreter refuses a venv whose version_info disagrees with TD's
        (embody_pyenv.py:1005), and an intact cfg keeps Start on the probe
        path instead of a full reinstall."""
        before = probe.read(self.td.cfg)
        broken, _ = probe.break_home(before, os.path.join(self.root, 'nope'))
        how = faults._break_venv(self.ctx, self.sm, self.st, self.td.cfg,
                                 before, broken)
        self.assertIn('displaced', how)
        self.assertEqual(probe.read(self.td.cfg), before)
        self.assertEqual(probe.read(self.td.interpreter), probe.STUB)
        self.assertFalse(self.sm['runs'](
            probe.venv_python(self.root, 'darwin')))
        self.assertEqual(len(self.st['displaced']), 1)

    def test_the_cfg_break_alone_would_have_proved_nothing_here(self):
        """The gap this closes: on darwin the first rung leaves an
        interpreter that still starts, which is what venv.skipped meant."""
        before = probe.read(self.td.cfg)
        broken, _ = probe.break_home(before, os.path.join(self.root, 'nope'))
        probe.write(self.td.cfg, broken)
        self.assertTrue(self.sm['runs'](
            probe.venv_python(self.root, 'darwin')))

    def test_a_failed_step_puts_the_displaced_interpreter_back(self):
        original = probe.read(self.td.interpreter)
        self.td.repair = False
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('venv.repair')
        self.assertEqual(probe.read(self.td.interpreter), original)
        self.assertTrue(self.sm['runs'](
            probe.venv_python(self.root, 'darwin')),
            'the uninstall leg must not inherit a dead interpreter')

    def test_neither_rung_taking_is_a_skip_not_a_red(self):
        self.td.venv_still_runs = True
        original = probe.read(self.td.interpreter)
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertStepOk('venv.skipped')
        self.assertIn('after both injections',
                      self.step('venv.skipped')['detail'])
        self.assertEqual(probe.read(self.td.interpreter), original)
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)

    def test_a_repair_that_leaves_the_interpreter_broken_fails(self):
        """'Venv interpreter repaired' is a log line; the proof is that the
        interpreter the BRIDGE launches starts again (the repair re-probes
        bin/python, envoy_setup probes bin/python3)."""
        original = probe.read(self.td.interpreter)
        self.td.repair_interpreter = False
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepOk('venv.repair')
        self.assertStepFailed('venv.runs_again')
        self.assertEqual(probe.read(self.td.interpreter), original,
                         'a failed step puts the interpreter back')

    def test_a_home_that_no_longer_exists_is_not_a_repaired_venv(self):
        """same_path falls back to a string compare for a home it cannot
        stat, and `home` is inert on posix -- so venv.runs_again is green
        and the cfg proof's isdir gate is the only thing left that catches
        a venv delegating to a TouchDesigner that is gone."""
        real = self.td.begin_repair

        def repair(*a, **k):
            os.rename(self.home, self.home + '.moved')   # TD moved meanwhile
            return real(good_home=self.home)
        self.td.begin_repair = repair
        self.assertFalse(faults._fault_venv(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepOk('venv.runs_again')
        self.assertStepFailed('venv.pyvenv_cfg')

    def test_a_proved_repair_drops_the_undo_it_no_longer_owns(self):
        """uv rewrote the interpreter and venv.runs_again proved it: a
        later _restore -- fault 4's, or run()'s error path -- must not put
        the displaced file back over the repair."""
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertEqual(self.st['displaced'], [])
        self.assertEqual(self.st['restore'], [])

    def test_the_injected_step_names_the_interpreter_it_displaced(self):
        """What the next macOS run has to show instead of venv.skipped."""
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertIn('displaced by a stub that exits 103',
                      self.step('venv.inject')['detail'])
        self.assertStepOk('venv.runs_again')


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
        self.td.window = lambda gen, up=18.0: None   # the kill never lands
        self.assertFalse(faults._fault_port(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('port.hold')

    def test_a_restart_landing_on_another_project_stops_the_leg(self):
        """Every re-point re-takes the target gate: the next injection must
        never go to a TouchDesigner that is not the smoke's."""
        real = self.td._start_now

        def elsewhere():
            real()
            self.td.project_folder = 'C:/Users/dev/Documents/Embody/dev'
        self.td._start_now = elsewhere
        self.assertFalse(faults._fault_port(self.ctx, self.rec, self.sm,
                                            self.st))
        self.assertStepFailed('leg.target')
        self.assertStepFailed('port.fallback')


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

    def test_a_renamed_private_name_fails_before_the_kill(self):
        """The kill script ASSIGNS _restart_count and friends: a rename
        would create a dead attribute and no error, and the leg would then
        credit the watchdog for an ordinary auto-restart."""
        self.td.attrs = False
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.kill')
        self.assertIn('no attribute', self.step('watchdog.kill')['detail'])

    def test_a_missing_envoy_running_flag_fails_before_the_kill(self):
        """The wedge is a storage write: a renamed key would leave the
        watchdog reviving a plain dead socket while the leg claims more."""
        self.td.running_flag = False
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.kill')

    def test_a_status_naming_another_port_fails(self):
        """'Running on port N' for a dead N is the wedge itself, not a
        recovery -- the port in the status has to be the live one."""
        real = self.td._start_now

        def stale_status():
            real()
            self.td.status = 'Running on port 9999'
        self.td._start_now = stale_status
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.running')


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

    def test_a_corruption_that_still_parses_is_refused(self):
        """The guard that makes the injection real: valid JSON would leave
        write_envoy_config nothing to prove."""
        real = faults._GARBAGE
        faults._GARBAGE = '{"instances": {}}'
        try:
            self.assertFalse(faults._fault_registry(self.ctx, self.rec,
                                                    self.sm, self.st))
        finally:
            faults._GARBAGE = real
        self.assertStepFailed('registry.inject')

    def test_a_row_that_is_not_a_dict_is_reported_not_raised(self):
        """instances only guarantees the container; a half-written row must
        not take the leg out through AttributeError."""
        real = self.td.write_registry
        self.td.write_registry = lambda port: probe.write(
            os.path.join(self.root, '.embody', 'envoy.json'),
            json.dumps({'instances': {'smoke': None, 'other': {
                'port': port, 'td_pid': self.td.pid}}}))
        try:
            self.assertTrue(faults._fault_registry(self.ctx, self.rec,
                                                   self.sm, self.st))
        finally:
            self.td.write_registry = real
        self.assertStepOk('registry.rewrite')

    def test_a_rewrite_under_another_pid_restores_the_registry(self):
        self.td.pid = 9999
        faults._fault_registry(self.ctx, self.rec, self.sm, self.st)
        self.assertStepFailed('registry.rewrite')
        self.assertEqual(probe.reg_ports(self.root), [self.ctx['port']],
                         'the copy kept aside must be put back')


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

    def test_running_out_of_budget_reads_as_inconclusive(self):
        """A slow runner must never red the release gate with a proof step
        blaming the product for what was the run clock."""
        # 310s at the door, under the reserve 27 fake-seconds later
        self.ctx['budget'] = lambda: 310.0 - self.td.now * 10
        self.td.repair = False           # nothing ever completes
        got = faults.run(self.ctx)
        self.assertFalse(got['ok'])
        self.assertIn('out of run budget', got['error'])
        self.assertEqual([s for s in got['steps'] if not s['ok']], [])
        self.assertEqual(probe.home_of(probe.read(self.td.cfg)), self.home)

    def test_too_little_budget_injects_nothing(self):
        self.ctx['budget'] = lambda: 120.0
        got = faults.run(self.ctx)
        self.assertFalse(got['ok'])
        self.assertIn('inconclusive', got['error'])
        self.assertEqual(got['steps'], [])

    def test_a_ctx_without_a_port_is_reported_not_raised(self):
        self.ctx['port'] = None
        got = faults.run(self.ctx)
        self.assertFalse(got['ok'])
        self.assertIn('TypeError', got['error'])
        self.assertEqual([s for s in got['steps'] if not s['ok']], [])

    def test_the_leg_cancels_its_own_timers_before_returning(self):
        """Whatever is still armed must no-op once the uninstall leg starts
        deleting the venv and the master switch it would flip."""
        got = faults.run(self.ctx)
        self.assertTrue(got['ok'], got)
        self.assertGreater(self.td.gen, 4)
        self.assertEqual(self.td.gen, self.td._gen_of(self.td.seen[-1]))
        self.assertTrue(any(s['step'] == 'leg.port' for s in got['steps']))

    def test_the_result_shape_matches_the_leg_contract(self):
        got = faults.run(self.ctx)
        self.assertEqual(sorted(got), ['error', 'ok', 'steps'])
        for step in got['steps']:
            self.assertEqual(sorted(step), ['detail', 'ok', 'step'])


class TestLegOnDarwin(TestLeg):
    """The whole leg on the macOS model -- fault 1 now injects there."""

    PLATFORM = 'darwin'


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

    def test_is_venv_command_accepts_another_spelling_of_the_run_dir(self):
        """launch_td realpaths the .toe, so TD writes the RESOLVED twin of
        the dir the leg was handed (macOS /var -> /private/var). A raw
        string compare reds venv.mcp_json on a healthy product."""
        cmd = probe.venv_python(self.root).replace('\\', '/')
        self.assertTrue(probe.is_venv_command(
            cmd, os.path.join(self.root, 'sub', '..')))
        self.assertFalse(probe.is_venv_command(
            cmd, os.path.join(self.root, 'sub')))

    def test_same_path_resolves_a_link_a_string_compare_would_miss(self):
        """The fold is the FALLBACK, not the answer: two spellings of one
        directory compare unequal by normcase and equal by samefile."""
        alias = os.path.join(self.root, 'td-bin-alias')
        try:
            os.symlink(self.home, alias, target_is_directory=True)
        except OSError:
            self.skipTest('this box refuses symlinks')
        self.assertNotEqual(os.path.normcase(os.path.abspath(alias)),
                            os.path.normcase(os.path.abspath(self.home)))
        self.assertTrue(probe.same_path(alias, self.home))
        self.assertFalse(probe.same_path(alias + '-gone', self.home))

    def test_displace_refuses_anything_outside_the_venv(self):
        """The posix interpreter is a symlink into TouchDesigner's own
        framework; a walk that escaped would rename that binary."""
        outside = os.path.join(self.root, 'fake-td-bin', 'python3')
        probe.write(outside, 'TD owns this')
        self.assertEqual(probe.displace(self.root, outside, probe.STUB), {})
        self.assertEqual(probe.read(outside), 'TD owns this')
        self.assertEqual(probe.displace(
            self.root, os.path.join(self.root, '.venv', 'nope'),
            probe.STUB), {})

    def test_displace_and_undisplace_round_trip_byte_exactly(self):
        target = os.path.join(self.root, '.venv', 'marker')
        probe.write(target, 'original\r\ncontent\n')
        rec = probe.displace(self.root, target, probe.STUB)
        self.assertTrue(rec)
        self.assertEqual(probe.read(target), probe.STUB)
        self.assertTrue(os.path.isfile(rec['backup']))
        self.assertTrue(probe.undisplace(rec))
        self.assertEqual(probe.read(target), 'original\r\ncontent\n',
                         'the round trip keeps CRLF, or _restore is a diff')

    def test_the_interpreter_walk_stops_at_the_venv_boundary(self):
        """bin/python3 -> python -> TD's framework binary: the walk returns
        the venv's own file and never the one outside run_dir."""
        bin_dir = os.path.join(self.root, '.venv', 'bin')
        os.makedirs(bin_dir, exist_ok=True)
        outside = os.path.join(self.root, 'fake-td-bin', 'python3.11')
        probe.write(outside, 'framework')
        real = os.path.join(bin_dir, 'python')
        try:
            os.symlink(outside, real)
            os.symlink('python', os.path.join(bin_dir, 'python3'))
        except OSError:
            self.skipTest('this box refuses symlinks (the posix injection '
                          'is modelled with plain files in TestLegOnDarwin)')
        self.assertEqual(probe.venv_interpreter(self.root, 'darwin'),
                         os.path.abspath(real))

    def test_undisplace_rebuilds_a_link_whose_backup_is_gone(self):
        bin_dir = os.path.join(self.root, '.venv', 'bin')
        os.makedirs(bin_dir, exist_ok=True)
        probe.write(os.path.join(bin_dir, 'python'), 'base')
        link = os.path.join(bin_dir, 'python3')
        try:
            os.symlink('python', link)
        except OSError:
            self.skipTest('this box refuses symlinks')
        rec = probe.displace(self.root, link, probe.STUB)
        self.assertEqual(rec['link'], 'python')
        os.rename(rec['backup'], rec['backup'] + '.lost')
        self.assertTrue(probe.undisplace(rec))
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), 'python')

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

    def test_until_raises_on_the_runs_own_ceiling(self):
        """A leg that outran budget() would take teardown down with it --
        and "the clock ran out" must not return the same None as "the
        product never did it"."""
        self.ctx['budget'] = lambda: 10.0      # below RESERVE_S
        with self.assertRaises(probe.BudgetExhausted):
            probe.until(self.ctx, self.sm, lambda: False, 60.0)
        self.ctx['budget'] = lambda: 100000.0
        self.assertIsNone(probe.until(self.ctx, self.sm, lambda: False, 0.0),
                          'a plain timeout still returns None')

    def test_venv_python_matches_what_envoy_setup_probes(self):
        for plat, tail in (('win32', 'Scripts/python.exe'),
                           ('darwin', 'bin/python3')):
            got = probe.venv_python(self.root, plat).replace('\\', '/')
            self.assertTrue(got.startswith(self.root.replace('\\', '/')))
            self.assertIn(tail, got)
        self.assertEqual(probe.venv_python(self.root),
                         probe.venv_python(self.root, sys.platform))

    def test_runs_reports_false_for_an_interpreter_that_is_not_one(self):
        self.assertFalse(probe.runs(os.path.join(
            self.root, '.venv', 'Scripts', 'python.exe')))
        self.assertFalse(probe.runs(os.path.join(self.root, 'no-such-exe')))

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


class TestWatchdogProof(_Case):
    """Fault 3 proves the WATCHDOG revived the socket, not the restart
    backoff. The proof is _reviveDeadServer's line, which only
    _watchdogTick reaches (EnvoyExt.py:6575, 6593 -> 6687), never the
    branch that happened to notice."""

    def test_the_status_stuck_branch_is_also_proof(self):
        # Live 2026-09-20: _scheduleRestart rewrote Envoystatus ~1s after
        # Stop(), so the tick found a stuck status, not a stale one. The
        # watchdog still revived it; pinning the other branch went red.
        self.td.watchdog_branch = 'status stuck'
        self.assertTrue(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                               self.st))
        self.assertStepOk('watchdog.revive')
        self.assertIn('status stuck', self.step('watchdog.revive')['detail'])

    def test_a_branch_line_without_the_revive_line_is_not_proof(self):
        self.td.watchdog_revive_logs = False
        self.td.watchdog_branch = 'status stuck'
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.revive')


class TestWedgeSurvivesTheExitHook(_Case):
    """The macOS outage: Stop()'s own exit hook landed after the kill
    script, cleared envoy_running and left Envoystatus 'Disabled', so the
    watchdog correctly refused to revive and the leg waited 180s for
    nothing (CI 2026-09-20)."""

    def test_a_clobbered_wedge_is_won_back_before_the_tick(self):
        self.td.clobber_wedge_at = 4.0
        self.assertTrue(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                               self.st))
        self.assertStepOk('watchdog.revive')

    def test_a_wedge_that_can_never_be_won_back_fails_loudly(self):
        self.td.clobber_wedge_at = 4.0
        self.td.wedge_reasserts = ()          # nothing puts it back
        self.assertFalse(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                                self.st))
        self.assertStepFailed('watchdog.revive')

    def test_the_failure_says_what_the_wedge_saw(self):
        crumb = os.path.join(self.root, faults._WEDGE_CRUMB % 5)
        probe.write(crumb, '{"at": 2, "gen": 5, "saw": "Disabled", '
                           '"wedged": false}' + chr(10))
        why = faults._why_no_revive(self.root, 5)
        self.assertIn('0/1', why)
        self.assertIn('Disabled', why)

    def test_no_breadcrumb_at_all_is_named(self):
        self.assertIn('never ran', faults._why_no_revive(self.root, 77))


class TestDisplaceIsAtomicEnough(_Case):
    """displace() renames the interpreter aside before writing the stub. A
    failure between the two used to return {} -- telling the caller there
    was nothing to undo -- and left the venv with no interpreter at all."""

    def _victim(self):
        path = os.path.join(self.root, '.venv', 'Scripts', 'python.exe')
        probe.write(path, 'the real interpreter')
        return path

    def test_a_failed_stub_write_puts_the_interpreter_back(self):
        path = self._victim()
        real = probe.read(path)
        orig = probe.write

        def boom(p, text):
            if p == path:
                raise OSError('no room')
            return orig(p, text)
        probe.write = boom
        try:
            self.assertEqual(probe.displace(self.root, path, 'stub'), {})
        finally:
            probe.write = orig
        self.assertTrue(os.path.isfile(path), 'interpreter was left missing')
        self.assertEqual(probe.read(path), real)

    def test_a_rename_that_cannot_happen_reports_no_injection(self):
        missing = os.path.join(self.root, '.venv', 'Scripts', 'absent.exe')
        self.assertEqual(probe.displace(self.root, missing, 'stub'), {})

    def test_it_refuses_anything_outside_the_venv(self):
        outside = os.path.join(self.root, 'not_the_venv.txt')
        probe.write(outside, 'mine')
        self.assertEqual(probe.displace(self.root, outside, 'stub'), {})
        self.assertEqual(probe.read(outside), 'mine')


class TestWedgeStandsDownAfterTheRevive(_Case):
    """Live 2026-09-20: the watchdog revived on 9871 and a later
    re-assertion wrote 'Running on port 9872' -- the port it had killed --
    back over it, so watchdog.running failed on a healthy Envoy."""

    def test_a_revive_onto_another_port_is_not_overwritten(self):
        self.td.move_port = True              # the revive lands elsewhere
        self.td.wedge_reasserts = (3.0, 5.0, 20.0, 30.0)   # well past it
        self.assertTrue(faults._fault_watchdog(self.ctx, self.rec, self.sm,
                                               self.st))
        self.assertStepOk('watchdog.running')
        detail = self.step('watchdog.running')['detail']
        self.assertIn(str(self.st['port']), detail)

    def test_the_status_names_the_port_the_tool_call_reached(self):
        self.td.move_port = True
        faults._fault_watchdog(self.ctx, self.rec, self.sm, self.st)
        step = self.step('watchdog.running')
        self.assertTrue(step['ok'], step['detail'])


class TestReadsSurviveADisturbedServer(_Case):
    """A repair or a restart closes the socket between two steps. A bare
    ctx['py'] then raises URLError out of the leg instead of waiting for
    the server it just disturbed (macOS CI 2026-09-20, Errno 61)."""

    def test_ask_waits_out_a_refused_connection(self):
        calls = {'n': 0}

        def flaky(code, timeout=30):
            calls['n'] += 1
            if calls['n'] < 3:
                raise OSError('Connection refused')
            return 'answered'
        ctx = dict(self.ctx, py=flaky)
        self.assertEqual(probe.ask(ctx, self.sm, 'x', 60.0), 'answered')
        self.assertGreaterEqual(calls['n'], 3)

    def test_ask_returns_its_default_when_it_never_answers(self):
        def dead(code, timeout=30):
            raise OSError('Connection refused')
        ctx = dict(self.ctx, py=dead)
        self.assertEqual(probe.ask(ctx, self.sm, 'x', 5.0, default='(none)'),
                         '(none)')

    def test_the_venv_fault_survives_a_repair_that_drops_the_socket(self):
        """The macOS shape: the repair restarts the server, so the read of
        the repair state lands on a closed socket."""
        real = self.td.py
        state = {'drop': 2}

        def dropping(code, timeout=30):
            if 'sorted(getattr' in code and state['drop'] > 0:
                state['drop'] -= 1
                raise OSError('Connection refused')
            return real(code, timeout)
        self.td.py = dropping
        self.ctx['py'] = dropping
        self.assertTrue(faults._fault_venv(self.ctx, self.rec, self.sm,
                                           self.st))
        self.assertStepOk('venv.repair')


class TestAskFollowsAMovedServer(_Case):
    """A restart can MOVE Envoy. On macOS the repair bounced it
    9870 -> 9871 -> 9870, and a reader pinned to the old port waited out
    its whole budget against a healthy server (CI 2026-09-20)."""

    def test_ask_re_points_at_the_registered_port(self):
        moved = {'to': 9873}
        self.td.write_registry(moved['to'])
        self.td.listening = {moved['to']}

        def only_on_new_port(code, timeout=30):
            if self.ctx['port'] != moved['to']:
                raise OSError('Connection refused')
            return 'answered'
        self.ctx['py'] = only_on_new_port
        self.assertEqual(probe.ask(self.ctx, self.sm, 'x', 60.0), 'answered')
        self.assertEqual(self.ctx['port'], moved['to'])

    def test_ask_gives_up_when_no_registered_port_answers(self):
        def never(code, timeout=30):
            raise OSError('Connection refused')
        self.ctx['py'] = never
        self.assertEqual(probe.ask(self.ctx, self.sm, 'x', 5.0, default='(none)'),
                         '(none)')
