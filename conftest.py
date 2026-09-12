"""Pytest-tier process-kill fence and per-run temp sandbox.

At the repo root so it loads for EVERY pytest.ini testpaths entry
(dev/embody/unit_tests, Collection/tests, dev/convoy) and for any single
file run under this rootdir. Never imported inside TouchDesigner: the bridge
test modules stub kill_stale_bridges and quit_td themselves for the in-TD
runner. The unit_tests and convoy conftests abort a run that skipped this.

Why: the in-TD bridge suite left a temp-dir heartbeat naming TD's own pid
(time patched to 1000), and a later pytest run's real kill_stale_bridges
taskkill'd it -- TD died (proven in envoy-bridge.log 2026-08-23 and
2026-08-29, inferred 2026-09-11). Contract: a test run may terminate only
processes it spawned.

- subprocess.Popen (so run/call/check_*/os.popen) and os.system refuse a
  kill command (taskkill, tskill, kill, pkill, killall, Stop-Process, wmic
  delete, ...) unless every target pid was spawned this session; name,
  filter, group, service-stop (schtasks /End, launchctl bootout, sc/net/
  systemctl stop) and osascript app-quit kills are always refused, through
  env/sudo/timeout-style wrappers and powershell -EncodedCommand too.
  Refusal happens BEFORE any process starts.
- os.kill/os.killpg likewise. win32 signals 0 and 1 are refused outright
  (CTRL_C/CTRL_BREAK: console-wide, and 0 reaches TerminateProcess); POSIX
  signal 0 is a harmless probe.
- tempfile, TEMP/TMP/TMPDIR and pytest's tmp_path root point at a per-run
  dir (removed at the end, path-checked), so no run sees another run's or a
  TD's heartbeat files.
- Every refusal is also recorded: a refusal swallowed by `except Exception`
  in the code under test still fails the session at the end, as does a
  bridge heartbeat left under this pid.
Residuals (unwrappable; every tier caller acts only on its own children or
injects a fake): pid reuse of a reaped own child; ctypes/_winapi
TerminateProcess and Job Object teardown (convoy_lifecycle, convoy_hostops,
Popen.kill on win32); any kill made INSIDE a spawned child or script
(python -c, powershell -File/stdin, a spawned convoy host).
"""

from __future__ import annotations

import base64
import inspect
import operator
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

_RUN_PREFIX = 'embody-pt-'
_FENCE_ATTR = '_embody_kill_fence'
_TEMP_VARS = ('TEMP', 'TMP', 'TMPDIR')
_SHELL_EXES = frozenset(('cmd', 'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish',
                         'powershell', 'pwsh', 'wsl', 'wmic'))
_SHELL_KILL = re.compile(
    r'(?i)(?<![\w-])(taskkill|tskill|pkill|killall|kill|stop-process|spps'
    r'|terminate)(?![\w-])')
_NAME_KILL = re.compile(
    r'(?i)(?<![\w-])(/im|/fi|-name|-processname|-inputobject|pkill'
    r'|killall)(?![\w-])')
_PID = re.compile(r'(?<![\w.-])\d+(?![\w.])')
_WMIC_DELETE = re.compile(r'(?i)\bwmic\b.*\bprocess\b.*\bdelete\b')
_WMIC_BY_NAME = re.compile(
    r'(?i)\bwmic\b.*\b(name|caption|commandline|executablepath|like)\b')
# A supervised service stop names no pid, so it is never verifiable.
_SERVICE_EXES = frozenset(('schtasks', 'launchctl', 'sc', 'net', 'systemctl'))
_SERVICE_STOP = re.compile(
    r'(?i)(?<![\w-])(schtasks\b.*\s[/-]end\b'
    r'|launchctl\s+(bootout|kill|stop|remove|unload)\b'
    r'|launchctl\s+kickstart\b.*\s-k\b'
    r'|(sc|net)(\.exe)?\s+stop\b'
    r'|systemctl\b.*\s(stop|kill|restart)\b'
    r'|stop-(service|scheduledtask)\b)')
# An app-wide quit (the AppleScript event or Cmd-Q) is pid-blind.
_APP_QUIT = re.compile(
    r'(?i)\bquit\b|keystroke\s+"q"\s+using\s+\{?\s*command\s+down')
# Wrapper -> its value-taking options; the wrapped command follows them.
_WRAPPERS: Dict[str, Tuple[str, ...]] = {
    'env': ('-u', '-C', '-S', '--unset', '--chdir', '--split-string'),
    'sudo': ('-u', '-g', '-C', '-D', '-h', '-p', '-r', '-t', '-U'),
    'doas': ('-u', '-C'), 'nice': ('-n', '--adjustment'), 'nohup': (),
    'setsid': (), 'time': (), 'stdbuf': ('-i', '-o', '-e'),
    'timeout': ('-s', '-k', '--signal', '--kill-after'),
    'xargs': ('-a', '-d', '-E', '-I', '-L', '-n', '-P', '-s', '--arg-file',
              '--delimiter', '--max-args', '--max-procs'),
}

Verdict = Tuple[bool, Optional[List[int]]]


def _exe_name(path: Any) -> str:
    name = re.split(r'[\\/]', os.fsdecode(path).strip().strip('"\''))[-1]
    name = name.lower()
    return name[:-4] if name.endswith(('.exe', '.com')) else name


def _tokens(text: str) -> List[str]:
    try:
        parts = shlex.split(text, posix=(os.name != 'nt'))
    except ValueError:
        parts = text.split()
    return [p.strip('"\'') for p in parts]


def _taskkill_pids(rest: List[str]) -> Optional[List[int]]:
    pids = []
    i = 0
    while i < len(rest):
        flag = rest[i].upper().replace('-', '/', 1)
        if flag in ('/IM', '/FI', '/S'):
            return None  # by name, by filter, or on another machine
        if flag == '/PID':
            if i + 1 >= len(rest) or not rest[i + 1].isdigit():
                return None
            pids.append(int(rest[i + 1]))
            i += 2
            continue
        i += 1
    return pids or None


def _posix_kill_verdict(rest: List[str]) -> Verdict:
    if rest and rest[0] in ('-l', '-L', '--list', '--table'):
        return False, None  # lists signal names, kills nothing
    i = 0
    if rest and rest[0] in ('-s', '-n', '--signal'):
        i = 2
    elif rest and rest[0].startswith('-') and rest[0] != '--':
        i = 1
    if i < len(rest) and rest[i] == '--':
        i += 1
    targets = rest[i:]
    if targets and all(t.isdigit() and int(t) > 0 for t in targets):
        return True, [int(t) for t in targets]
    return True, None  # job specs, process groups, names: never verifiable


def _unwrap(exe: str, rest: List[str]) -> List[str]:
    """The argv a wrapper (env, sudo, timeout, xargs, ...) runs."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == '--':
            return rest[i + 1:]
        if tok.startswith('-'):
            i += 2 if tok in _WRAPPERS[exe] else 1
        elif exe == 'env' and '=' in tok:
            i += 1
        elif exe == 'timeout' and re.fullmatch(r'[\d.]+[smhd]?', tok):
            return rest[i + 1:]
        else:
            return rest[i:]
    return []


def _encoded_script(argv: List[str]) -> Tuple[Optional[str], str]:
    """(script, blob) behind powershell -EncodedCommand (any prefix, -ec).

    ('', '') when there is none; (None, blob) when the blob is not base64
    UTF-16LE, which the caller refuses because it cannot be read. Scanning
    stops at -Command/-File: what follows is script text or script args.
    '-co' is ambiguous with -ConfigurationName, so it keeps scanning.
    """
    for i, tok in enumerate(argv[1:], 1):
        flag = tok[1:].lower() if tok[:1] in ('-', '/') else ''
        if flag == 'ec' or (flag and 'encodedcommand'.startswith(flag)):
            blob = argv[i + 1] if i + 1 < len(argv) else ''
            try:
                return (base64.b64decode(blob, validate=True)
                        .decode('utf-16-le'), blob)
            except ValueError:  # binascii.Error and UnicodeDecodeError
                return None, blob
        if flag and (flag == 'c' or 'file'.startswith(flag)
                     or (len(flag) >= 3 and 'command'.startswith(flag))):
            break
    return '', ''


def classify_command(args: Any, executable: Any = None,
                     shell: bool = False) -> Verdict:
    """(is_kill, pids) for a subprocess command; pids None = always refuse."""
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        argv = _tokens(text)
    else:
        argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike))
                else str(a) for a in args]
        text = ' '.join(argv)
    if not argv:
        return False, None
    exe = _exe_name(executable if executable else argv[0])
    if exe in _WRAPPERS and not shell:
        inner = _unwrap(exe, argv[1:])
        is_kill, pids = classify_command(inner) if inner else (False, None)
        # xargs reads its targets from stdin: never verifiable.
        return (True, None) if is_kill and exe == 'xargs' else (is_kill, pids)
    if exe in ('powershell', 'pwsh'):
        script, blob = _encoded_script(argv)
        if script is None:
            return True, None
        if blob:
            text = text.replace(blob, ' ') + ' ' + script
    if ((shell or exe in _SHELL_EXES or exe in _SERVICE_EXES)
            and _SERVICE_STOP.search(text)):
        return True, None
    if ((exe == 'osascript' or ((shell or exe in _SHELL_EXES)
                                and re.search(r'(?i)\bosascript\b', text)))
            and _APP_QUIT.search(text)):
        return True, None
    if shell or exe in _SHELL_EXES:
        if not (_SHELL_KILL.search(text) or _WMIC_DELETE.search(text)):
            return False, None
        if exe == 'wmic' and not re.search(r'(?i)\bprocess\b', text):
            return False, None
        if _NAME_KILL.search(text) or _WMIC_BY_NAME.search(text):
            return True, None
        return True, [int(p) for p in _PID.findall(text)] or None
    if exe in ('pkill', 'killall'):
        return True, None
    if exe == 'taskkill':
        return True, _taskkill_pids(argv[1:])
    if exe == 'tskill':
        first = next((t for t in argv[1:] if not t.startswith('/')), '')
        return True, ([int(first)] if first.isdigit() else None)
    if exe == 'kill':
        return _posix_kill_verdict(argv[1:])
    return False, None


class KillFence:
    """Session state: own pids, the per-run temp dir, recorded refusals."""

    classify = staticmethod(classify_command)  # reachable from the tests

    def __init__(self) -> None:
        self.spawned: set = set()
        self.refusals: List[str] = []
        self.real_temp: str = ''
        self.run_dir: str = ''
        self.owner: Any = None  # the pytest config that installed it
        self._saved: Dict[Any, Any] = {}

    # -- decisions ------------------------------------------------------

    def owns(self, pid: int) -> bool:
        return pid in self.spawned

    def refuse(self, what: str) -> None:
        msg = ('kill fence: refused %s -- a test may only terminate '
               'processes it spawned (repo-root conftest.py)' % what)
        self.refusals.append(msg)
        raise AssertionError(msg)

    def check_command(self, args: Any, executable: Any = None,
                      shell: bool = False) -> None:
        is_kill, pids = classify_command(args, executable, shell)
        if not is_kill:
            return
        if pids is None:
            self.refuse('%r (name/filter/group kills are never allowed)'
                        % (args,))
        foreign = [p for p in pids if not self.owns(p)]
        if foreign:
            self.refuse('%r (pid(s) %s not spawned by this run)'
                        % (args, foreign))

    def check_signal(self, pid: Any, sig: Any, group: bool) -> None:
        what = 'os.%s(%r, %r)' % ('killpg' if group else 'kill', pid, sig)
        # The real call converts both through __index__ (a numpy int is a
        # pid); anything else raises TypeError here, before the real call.
        pid, sig = operator.index(pid), operator.index(sig)
        if sys.platform == 'win32' and not group and sig in (0, 1):
            # CTRL_C_EVENT/CTRL_BREAK_EVENT go to GenerateConsoleCtrlEvent,
            # which can reach every process on the console; 0 also reaches
            # TerminateProcess. Neither is a probe nor pid-scoped.
            self.refuse(what + ' (win32 console control event)')
        if sig == 0:
            return  # POSIX: a harmless liveness probe
        target = -pid if (not group and pid < 0) else pid
        if target <= 0 or not self.owns(target):
            self.refuse(what)

    def safe_to_remove(self, path: str) -> bool:
        """Only the exact dir this fence created, directly under real_temp."""
        if not path or not self.run_dir or not self.real_temp:
            return False
        norm = os.path.normcase(os.path.abspath(path))
        return (norm == os.path.normcase(os.path.abspath(self.run_dir))
                and os.path.basename(norm).startswith(_RUN_PREFIX)
                and os.path.dirname(norm)
                == os.path.normcase(os.path.abspath(self.real_temp))
                and os.path.isdir(path) and not os.path.islink(path))

    # -- install / uninstall -------------------------------------------

    def install(self) -> None:
        self.real_temp = tempfile.gettempdir()
        self.run_dir = tempfile.mkdtemp(prefix=_RUN_PREFIX, dir=self.real_temp)
        self._saved['tempdir'] = tempfile.tempdir
        self._saved['env'] = {k: os.environ.get(k) for k in _TEMP_VARS}
        tempfile.tempdir = self.run_dir
        for key in _TEMP_VARS:
            os.environ[key] = self.run_dir
        self._wrap_popen()
        self._wrap(os, 'kill', lambda orig: self._signal_wrapper(orig, False))
        if hasattr(os, 'killpg'):
            self._wrap(os, 'killpg',
                       lambda orig: self._signal_wrapper(orig, True))
        self._wrap(os, 'system', self._system_wrapper)

    def _wrap(self, owner: Any, name: str,
              factory: Callable[[Callable[..., Any]], Callable[..., Any]]
              ) -> None:
        orig = getattr(owner, name)
        wrapper = factory(orig)
        setattr(wrapper, _FENCE_ATTR, self)
        self._saved[(owner, name)] = orig
        setattr(owner, name, wrapper)

    def _wrap_popen(self) -> None:
        # Patch __init__ on the base class, not the name: every subclass
        # (asyncio's included) and run/call/check_* go through it.
        fence = self
        orig = subprocess.Popen.__init__
        sig = inspect.signature(orig)

        def fenced_init(popen: Any, *args: Any, **kwargs: Any) -> None:
            try:
                bound = sig.bind(popen, *args, **kwargs)
            except TypeError:
                return orig(popen, *args, **kwargs)  # raises the real error
            argv = bound.arguments.get('args')
            if isinstance(argv, Iterator):
                argv = bound.arguments['args'] = list(argv)  # checked == run
            fence.check_command(argv, bound.arguments.get('executable'),
                                bool(bound.arguments.get('shell')))
            orig(*bound.args, **bound.kwargs)
            pid = getattr(popen, 'pid', None)
            if isinstance(pid, int):
                fence.spawned.add(pid)

        setattr(fenced_init, _FENCE_ATTR, self)
        self._saved[(subprocess.Popen, '__init__')] = orig
        subprocess.Popen.__init__ = fenced_init

    def _signal_wrapper(self, orig: Callable[[int, int], None],
                        group: bool) -> Callable[[int, int], None]:
        def fenced(pid: int, sig: int) -> None:
            self.check_signal(pid, sig, group)
            return orig(pid, sig)
        return fenced

    def _system_wrapper(self, orig: Callable[[str], int]
                        ) -> Callable[[str], int]:
        def fenced(command: str) -> int:
            self.check_command(command, None, True)
            return orig(command)
        return fenced

    def uninstall(self) -> None:
        for key, orig in list(self._saved.items()):
            if isinstance(key, tuple):
                owner, name = key
                setattr(owner, name, orig)
        tempfile.tempdir = self._saved.get('tempdir')
        for key, value in self._saved.get('env', {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.safe_to_remove(self.run_dir):
            shutil.rmtree(self.run_dir, ignore_errors=True)


_fence: Optional[KillFence] = None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """tryfirst: before any other conftest makes a temp dir or a process."""
    global _fence
    if getattr(subprocess.Popen.__init__, _FENCE_ATTR, None) is not None:
        return  # a nested/in-process session: the outer fence already holds
    _fence = KillFence()
    _fence.install()
    _fence.owner = config
    # tmp_path root straight in the run dir, not the run dir PLUS
    # pytest-of-<user>/pytest-N: that nesting pushed a git-init test past
    # Windows MAX_PATH under a deep TEMP (2026-09-11). Set before tmpdir's own
    # configure reads it (tryfirst); a fresh subdir, since pytest rm_rf's it.
    if config.option.basetemp is None:
        config.option.basetemp = os.path.join(_fence.run_dir, 't')


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Tripwires: a refusal the code under test swallowed, or a bridge
    heartbeat left under THIS pid (a stale one is what killed TD)."""
    if _fence is None:
        return
    beat = os.path.join(_fence.run_dir, 'envoy-bridge-%d.heartbeat'
                        % os.getpid())
    if os.path.exists(beat):  # the run dir is fresh: this run wrote it
        session.exitstatus = 1
        print('\nKILL FENCE: %s exists -- a bridge heartbeat naming this '
              'pid is what a later stale-bridge cleanup killed' % beat)
    if not _fence.refusals:
        return
    session.exitstatus = 1
    print('\nKILL FENCE: %d refused kill(s) this run -- code under test tried '
          'to terminate a process it did not spawn:\n  %s'
          % (len(_fence.refusals), '\n  '.join(_fence.refusals[:10])))


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """Only the session that installed the fence removes it: a nested
    in-process session must not unfence the outer one or delete its dir."""
    global _fence
    if _fence is not None and _fence.owner is config:
        _fence.uninstall()
        _fence = None
