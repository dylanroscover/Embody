"""
Test suite: the pytest-tier process-kill fence (repo-root conftest.py).

The fence is a conftest, so this whole suite skips inside TouchDesigner.
Every command the fence must refuse runs with Popen._execute_child replaced
by a sentinel, so even a broken fence starts nothing; os.kill/os.system
refusals target a pid that cannot exist. Only children spawned here die.
"""

import base64
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import types
from collections.abc import Iterator
from unittest.mock import patch

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_NO_SUCH_PID = 2 ** 31 - 4  # never live, so a regressed fence kills nothing
_PREFIX = 'embody-pt-'


def _fence() -> object:
    return getattr(subprocess.Popen.__init__, '_embody_kill_fence', None)


def _encoded(script: str) -> str:
    """powershell -EncodedCommand payload: base64 of UTF-16LE."""
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def test_tmp_path_root_is_the_run_dir(tmp_path: object) -> None:
    """pytest-native (the in-TD runner collects classes only): tmp_path sits
    directly in the run dir -- the extra pytest-of-<user>/pytest-N depth
    pushed a git-init test past Windows MAX_PATH."""
    fence = _fence()
    assert fence is not None, 'kill fence NOT installed'
    # Resolved on both sides: pytest resolves the basetemp it is handed, so a
    # symlinked or 8.3-short TEMP spells the same directory two ways.
    root = os.path.normcase(os.path.realpath(os.path.join(fence.run_dir, 't')))
    got = os.path.normcase(os.path.realpath(str(tmp_path)))
    assert got.startswith(root + os.sep), (got, root)


class TestPytestKillFence(EmbodyTestCase):

    def setUp(self) -> None:
        super().setUp()
        if 'td' in sys.modules:
            self.skipTest('pytest tier only: the kill fence is a conftest')
        self.fence = _fence()
        self.assertIsNotNone(
            self.fence, 'kill fence NOT installed: the repo-root conftest.py '
            'did not load, and this run can kill processes it never spawned')
        self.children = []
        self.provoked = []

    def tearDown(self) -> None:
        for child in getattr(self, 'children', []):
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        fence = getattr(self, 'fence', None)
        for msg in getattr(self, 'provoked', []):
            # Only the refusals provoked on purpose are forgotten; any other
            # stays recorded and fails the run at the session-end tripwire.
            if fence is not None and msg in fence.refusals:
                fence.refusals.remove(msg)
        super().tearDown()

    def _spawn(self) -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        return child

    @contextlib.contextmanager
    def _refused(self) -> Iterator[None]:
        """The body raises AssertionError and records exactly one refusal."""
        before = len(self.fence.refusals)
        with self.assertRaises(AssertionError):
            yield
        self.assertEqual(len(self.fence.refusals), before + 1,
                         'every refusal is recorded for the tripwire')
        self.provoked.append(self.fence.refusals[-1])

    def _refused_before_start(self, *popen_args: object,
                              **popen_kwargs: object) -> None:
        """The command raises AssertionError and no process ever starts."""
        started = []

        def sentinel(*a: object, **k: object) -> None:
            started.append(a)
            raise RuntimeError('sentinel: a process would have started')

        with patch.object(subprocess.Popen, '_execute_child', sentinel):
            with self._refused():
                subprocess.run(*popen_args, **popen_kwargs)
        self.assertEqual(started, [], 'the fence must refuse BEFORE spawning')

    # -- installed ------------------------------------------------------

    def test_fence_is_installed_on_every_kill_surface(self) -> None:
        for fn in (subprocess.Popen.__init__, os.kill, os.system):
            self.assertIs(getattr(fn, '_embody_kill_fence', None), self.fence)
        if hasattr(os, 'killpg'):
            self.assertIs(getattr(os.killpg, '_embody_kill_fence', None),
                          self.fence)

    def test_temp_is_a_private_per_run_dir(self) -> None:
        run_dir = self.fence.run_dir
        self.assertEqual(tempfile.gettempdir(), run_dir)
        for key in ('TEMP', 'TMP', 'TMPDIR'):
            self.assertEqual(os.environ.get(key), run_dir, key)
        self.assertTrue(os.path.basename(run_dir).startswith(_PREFIX))
        self.assertEqual(os.path.normcase(os.path.dirname(run_dir)),
                         os.path.normcase(self.fence.real_temp))
        # The unit_tests conftest's project.folder sandbox is made after the
        # fence (tryfirst), so it is removed with the run instead of leaking.
        self.assertTrue(os.path.normcase(project.folder).startswith(
            os.path.normcase(run_dir)), project.folder)

    def test_end_of_run_removal_is_path_checked(self) -> None:
        safe = self.fence.safe_to_remove
        self.assertTrue(safe(self.fence.run_dir))
        self.assertFalse(safe(self.fence.real_temp))
        self.assertFalse(safe(os.path.dirname(self.fence.real_temp)))
        self.assertFalse(safe(os.path.join(self.fence.real_temp,
                                           _PREFIX + 'someone-else')))
        self.assertFalse(safe(os.path.join(self.fence.run_dir, 'child')))
        self.assertFalse(safe(''))

    def test_a_nested_session_never_tears_down_this_fence(self) -> None:
        """pytest_unconfigure for a config that did not install the fence
        (a nested in-process session) leaves it and its run dir alone."""
        self.assertIsNotNone(self.fence.owner)
        hooks = type(self.fence).install.__globals__
        with patch.object(type(self.fence), 'uninstall') as uninstall:
            try:
                hooks['pytest_unconfigure'](types.SimpleNamespace())
            finally:
                hooks['_fence'] = self.fence  # even if the guard regressed
        uninstall.assert_not_called()
        self.assertIs(_fence(), self.fence)

    # -- refused ----------------------------------------------------------

    def test_taskkill_of_an_unspawned_pid_is_refused_before_it_starts(self) -> None:
        self._refused_before_start(['taskkill', '/F', '/PID',
                                    str(os.getppid())])

    def test_name_filter_and_shell_wrapped_kills_are_refused(self) -> None:
        ppid = str(os.getppid())
        for argv in (['taskkill', '/F', '/IM', 'TouchDesigner.exe'],
                     ['taskkill', '/FI', 'IMAGENAME eq python*'],
                     [r'C:\Windows\System32\taskkill.exe', '/PID', ppid],
                     ['pkill', '-f', 'envoy_bridge'],
                     ['killall', 'TouchDesigner'],
                     ['kill', '-9', ppid],
                     ['tskill', ppid],
                     ['powershell', '-NoProfile', '-Command',
                      'Stop-Process -Id %s -Force' % ppid],
                     ['pwsh', '-Command', 'Get-Process python | Stop-Process'],
                     ['cmd', '/c', 'taskkill /F /PID %s' % ppid],
                     ['wmic', 'process', 'where', 'processid=' + ppid,
                      'delete'],
                     ['wmic', 'process', 'where', "name='x.exe'", 'delete'],
                     ['schtasks', '/End', '/TN', 'EmbodyConvoyHost'],
                     ['launchctl', 'bootout', 'gui/501/tools.embody.x'],
                     ['launchctl', 'kickstart', '-k', 'gui/501/x'],
                     ['sc', 'stop', 'svc'], ['net', 'stop', 'svc'],
                     ['systemctl', '--user', 'stop', 'x'],
                     ['pwsh', '-Command', 'Stop-ScheduledTask x'],
                     ['timeout', '5', 'kill', '-9', ppid],
                     ['env', 'A=1', 'taskkill', '/F', '/PID', ppid],
                     ['sudo', '-u', 'root', 'kill', ppid],
                     ['xargs', 'kill'],
                     ['powershell', '-EncodedCommand',
                      _encoded('Stop-Process -Id %s -Force' % ppid)],
                     ['powershell', '-enc', 'not base64 at all'],
                     ['osascript', '-e',
                      'tell application "TouchDesigner" to quit'],
                     ['sh', '-c', 'osascript -e \'tell app "TD" to quit\'']):
            with self.subTest(argv=argv):
                self._refused_before_start(argv)
        with self.subTest(shell=True):
            self._refused_before_start('taskkill /F /PID %s' % ppid,
                                       shell=True)

    def test_os_system_kill_is_refused(self) -> None:
        with self._refused():
            os.system('taskkill /F /PID %d' % _NO_SUCH_PID)

    def test_os_kill_of_an_unspawned_pid_is_refused(self) -> None:
        with self._refused():
            os.kill(_NO_SUCH_PID, signal.SIGTERM)

    def test_an_index_typed_pid_is_still_fenced(self) -> None:
        """os.kill takes any __index__ pid (a numpy int): so does the fence."""
        class IndexLike:
            def __index__(self) -> int:
                return _NO_SUCH_PID

        with self._refused():
            os.kill(IndexLike(), signal.SIGTERM)

    def test_win32_signal_zero_is_refused_even_for_own_child(self) -> None:
        if sys.platform != 'win32':
            self.skipTest('POSIX signal 0 is a harmless liveness probe')
        child = self._spawn()
        with self._refused():
            os.kill(child.pid, 0)
        self.assertIsNone(child.poll(), 'os.kill(pid, 0) must not have run')

    def test_win32_ctrl_break_is_refused_even_for_own_child(self) -> None:
        """CTRL_BREAK_EVENT goes to GenerateConsoleCtrlEvent, which can
        reach every process sharing the console, not just the target."""
        if sys.platform != 'win32':
            self.skipTest('signal 1 is SIGHUP on POSIX: an ordinary signal')
        child = self._spawn()
        with self._refused():
            os.kill(child.pid, signal.CTRL_BREAK_EVENT)
        self.assertIsNone(child.poll(), 'the event must not have been sent')

    def test_posix_signal_zero_probe_is_allowed(self) -> None:
        if sys.platform == 'win32':
            self.skipTest('win32 signal 0 reaches TerminateProcess')
        with self.assertRaises(ProcessLookupError):
            os.kill(_NO_SUCH_PID, 0)

    def test_a_swallowed_refusal_still_reaches_the_tripwire(self) -> None:
        """Code under test often wraps kills in `except Exception: pass`."""
        before = len(self.fence.refusals)
        started = []
        with patch.object(subprocess.Popen, '_execute_child',
                          lambda *a, **k: started.append(a)):
            try:
                subprocess.run(['taskkill', '/PID', str(os.getppid())])
            except Exception:
                pass
        self.assertEqual(started, [])
        self.assertEqual(len(self.fence.refusals), before + 1)
        self.provoked.append(self.fence.refusals[-1])

    def test_session_end_tripwires_fail_the_run(self) -> None:
        """pytest_sessionfinish itself: a recorded refusal, or a bridge
        heartbeat under this pid in the run dir, sets exitstatus 1."""
        hook = type(self.fence).install.__globals__['pytest_sessionfinish']

        def finish() -> int:
            session = types.SimpleNamespace(exitstatus=0)
            with patch('sys.stdout', io.StringIO()):
                hook(session, 0)
            return session.exitstatus

        with patch.object(self.fence, 'refusals', []):
            self.assertEqual(finish(), 0, 'a clean run stays green')
            self.fence.refusals.append('provoked')
            self.assertEqual(finish(), 1, 'a recorded refusal fails the run')
        beat = os.path.join(self.fence.run_dir,
                            'envoy-bridge-%d.heartbeat' % os.getpid())
        with open(beat, 'w', encoding='utf-8') as f:
            f.write('{}')
        try:
            with patch.object(self.fence, 'refusals', []):
                self.assertEqual(finish(), 1, 'a leaked heartbeat fails it')
        finally:
            os.remove(beat)

    # -- allowed ----------------------------------------------------------

    def test_a_real_kill_of_its_own_child_is_allowed(self) -> None:
        child = self._spawn()
        if sys.platform == 'win32':
            argv = ['taskkill', '/F', '/PID', str(child.pid)]
        else:
            argv = ['kill', '-9', str(child.pid)]
        result = subprocess.run(argv, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(child.wait(timeout=10))

    def test_os_kill_of_its_own_child_is_allowed(self) -> None:
        child = self._spawn()
        os.kill(child.pid, signal.SIGTERM)
        self.assertIsNotNone(child.wait(timeout=10))

    def test_non_kill_commands_pass_untouched(self) -> None:
        for argv in (['git', 'commit', '-m', 'kill the stale bridge'],
                     ['powershell', '-NoProfile', '-Command',
                      'Get-CimInstance Win32_Process | Select-Object '
                      'ProcessId, ParentProcessId, CommandLine'],
                     ['kill', '-l'],
                     ['sh', '-c', 'echo skill-issue'],
                     ['schtasks', '/Query', '/TN', 'EmbodyConvoyHost'],
                     ['launchctl', 'print', 'gui/501/x'],
                     ['git', 'commit', '-m', 'sc stop and schtasks /End'],
                     ['timeout', '/t', '1'],
                     ['env', 'A=1', 'python', '-c', 'pass'],
                     ['xargs', 'echo'],
                     ['powershell', '-EncodedCommand', _encoded('Get-Date')],
                     ['cmd', '/c', 'reg query HKCU\\x'],
                     ['osascript', '-e', 'tell application "System Events" '
                      'to get name of every window']):
            with self.subTest(argv=argv):
                self.assertEqual(self.fence.classify(argv), (False, None))

    def test_flags_after_command_or_file_are_script_text(self) -> None:
        """A '-e' after -Command/-File is script text or a script arg, not
        -EncodedCommand (it was refused as an unreadable encoded kill)."""
        c = self.fence.classify
        for argv in (['powershell', '-Command', 'Get-ChildItem', '-e', 'x'],
                     ['powershell', '-c', 'Get-Date', '-ec', 'x'],
                     ['pwsh', '-File', 's.ps1', '-enc', 'x']):
            with self.subTest(argv=argv):
                self.assertEqual(c(argv), (False, None))
        self.assertEqual(c(['powershell', '-enc', 'not base64', '-c', 'x']),
                         (True, None), 'before -Command it is still encoded')
        self.assertEqual(c(['powershell', '-Command', 'Stop-Process', '-Id',
                            '12', '-e', 'x']), (True, [12]))

    def test_classifier_extracts_exact_target_pids(self) -> None:
        cases = {
            ('taskkill', '/F', '/PID', '12', '/T'): (True, [12]),
            ('taskkill', '/PID', '12', '/PID', '34'): (True, [12, 34]),
            ('kill', '-s', 'TERM', '12'): (True, [12]),
            ('kill', '--', '-12'): (True, None),
            ('tskill', 'notepad'): (True, None),
            ('powershell', 'Stop-Process', '-Id', '12'): (True, [12]),
            ('wmic', 'process', 'where', 'processid=12', 'delete'):
                (True, [12]),
            ('env', 'A=1', 'kill', '-9', '12'): (True, [12]),
            ('timeout', '-s', 'KILL', '5', 'taskkill', '/PID', '12'):
                (True, [12]),
            ('powershell', '-ec', _encoded('Stop-Process -Id 12')):
                (True, [12]),
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(self.fence.classify(list(argv)), expected)
