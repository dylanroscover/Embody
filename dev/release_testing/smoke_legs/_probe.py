"""
Probe kit for the smoke legs: reading the run directory, driving the smoke
TD's Envoy through ctx, and recording steps. Nothing here is specific to
one leg, and nothing here raises for an absent or malformed file -- a leg
asserts, this module only reports what it found.

Every environment touch (clock, sleep, sockets) goes through `seams(ctx)`
so the unit tests can drive a whole leg on a fake clock and fake ports:
ctx['_seams'] = {'clock', 'sleep', 'alive', 'hold', 'runs'}.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

BACKUP = 'faults_backup'
RESERVE_S = 45.0    # never spend the run's last seconds; teardown needs them


class BudgetExhausted(Exception):
    """The run ran out of clock while a leg was waiting. Raised instead of
    reported, so a slow runner reads as inconclusive and never as a product
    regression -- the two were one `None` return before (field 2026-09-19)."""


def alive(port) -> bool:
    """True iff something answers on 127.0.0.1:port."""
    try:
        with socket.create_connection(('127.0.0.1', int(port)), 0.4):
            return True
    except (OSError, ValueError):
        return False


def hold(port):
    """A listening socket on `port` held by THIS process, or None. On posix
    SO_REUSEADDR matches what the server binds with (asyncio defaults it on,
    uvicorn sets it): without it a TIME_WAIT TCB locks the leg out of a port
    Envoy could still take. Never on win32, where SO_REUSEADDR would let the
    bind succeed UNDER a live listener and the hold would prove nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != 'win32':
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', int(port)))
        s.listen(1)
    except (OSError, ValueError):
        s.close()
        return None
    return s


def seams(ctx) -> dict:
    s = ctx.get('_seams') or {}
    return {'clock': s.get('clock') or time.monotonic,
            'sleep': s.get('sleep') or time.sleep,
            'alive': s.get('alive') or alive,
            'hold': s.get('hold') or hold,
            'runs': s.get('runs') or runs}


# --- run_dir reads ---

def read(path) -> str:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def write(path, text) -> None:
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)


def backup(run_dir, path) -> str:
    """Copy `path` aside under run_dir/faults_backup, keeping its name."""
    dest = os.path.join(run_dir, BACKUP, os.path.basename(path))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(path, dest)
    return dest


def entries(path) -> list:
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def logs(run_dir) -> str:
    """Every log the smoke TD writes under run_dir, oldest first. TD rotates
    its log on the feature phase's save, so the newest file alone is not the
    session (same reason smoke_run.collect_logs reads them all)."""
    names = [os.path.join(run_dir, 'td-console.log'),
             os.path.join(run_dir, 'bootstrap.log')]
    folder = os.path.join(run_dir, 'logs')
    try:
        names += sorted((os.path.join(folder, n) for n in os.listdir(folder)
                         if n.endswith('.log')), key=os.path.getmtime)
    except OSError:
        pass
    out = []
    for path in names:
        try:
            out.append(read(path))
        except OSError:
            pass
    return '\n'.join(out)


def log_count(run_dir, needle) -> int:
    """How many times `needle` appears in the run's logs. Evidence is taken
    as a COUNT that has to grow, never a byte offset: TD starts a new log
    file on save, which reorders the concatenation and silently invalidates
    any offset taken before it."""
    return logs(run_dir).count(needle)


def registry(run_dir):
    """The parsed .embody/envoy.json, or None when missing or invalid."""
    try:
        data = json.loads(read(os.path.join(run_dir, '.embody', 'envoy.json')))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def instances(run_dir) -> dict:
    rows = (registry(run_dir) or {}).get('instances')
    return rows if isinstance(rows, dict) else {}


def reg_ports(run_dir) -> list:
    out = []
    for info in instances(run_dir).values():
        try:
            out.append(int(info.get('port')))
        except (TypeError, ValueError, AttributeError):
            pass
    return out


def mcp_command(run_dir) -> str:
    """The interpreter .mcp.json launches the Envoy bridge with."""
    try:
        data = json.loads(read(os.path.join(run_dir, '.mcp.json')))
        return str(data['mcpServers']['envoy']['command'])
    except (OSError, ValueError, KeyError, TypeError):
        return ''


def is_venv_command(cmd, run_dir) -> bool:
    """Whether .mcp.json points back at THIS run's own venv interpreter."""
    c = str(cmd).replace('\\', '/').lower()
    root = str(run_dir).replace('\\', '/').rstrip('/').lower()
    return c.startswith(root + '/') and '/.venv/' in c


def venv_paths(run_dir) -> tuple:
    """(pyvenv.cfg, site-packages) inside the run's venv, either platform."""
    venv = os.path.join(run_dir, '.venv')
    site = os.path.join(venv, 'Lib', 'site-packages')       # win32
    for name in ([] if os.path.isdir(site) else entries(venv + '/lib')):
        if os.path.isdir(os.path.join(venv, 'lib', name, 'site-packages')):
            site = os.path.join(venv, 'lib', name, 'site-packages')
            break
    return os.path.join(venv, 'pyvenv.cfg'), site


def venv_python(run_dir) -> str:
    """The venv interpreter envoy_setup probes (envoy_setup.py:136-140)."""
    venv = os.path.join(run_dir, '.venv')
    return (os.path.join(venv, 'Scripts', 'python.exe')
            if sys.platform == 'win32'
            else os.path.join(venv, 'bin', 'python3'))


def runs(python_path, timeout=10.0) -> bool:
    """Whether that interpreter still starts -- the same one-liner
    probe_venv_python runs, from the orchestrator process. Lets a fault
    verify it is injectable on THIS platform instead of assuming it."""
    try:
        return subprocess.run(
            [str(python_path), '-c', 'import sys; print(sys.version)'],
            capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def home_of(text) -> str:
    """pyvenv.cfg's `home` -- the base interpreter the venv delegates to."""
    for line in str(text).splitlines():
        if '=' in line and line.split('=')[0].strip() == 'home':
            return line.split('=', 1)[1].strip()
    return ''


def break_home(text, bogus) -> tuple:
    """(pyvenv.cfg text with `home` pointed nowhere, whether one was found).
    Every other line is kept: repair_venv_interpreter refuses a venv whose
    version_info disagrees with TD's."""
    out, hit = [], False
    for line in str(text).splitlines(True):
        keep = hit or '=' not in line or line.split('=')[0].strip() != 'home'
        out.append(line if keep else 'home = %s\n' % bogus)
        hit = hit or not keep
    return ''.join(out), hit


# --- driving the smoke TD ---

def defer(ctx, script, delay_ms=1500):
    """Hand `script` to the smoke TD's main thread and return at once --
    mandatory for anything that stops the server, since the reply to this
    very call travels over the socket the script is about to close. `run` is
    not in execute_python's namespace (EnvoyExt._execNamespace), hence the
    import; the scheduled string runs in a normal TD script context, on
    wall time because every deadline a leg holds is time.monotonic."""
    return ctx['py']("from td import run as _run\n"
                     "_run(%r, fromOP=op.Embody, delayMilliSeconds=%d,\n"
                     "     wallTime=True)\n"
                     "result = 'scheduled'" % (script, int(delay_ms)))


def until(ctx, sm, pred, timeout, poll=1.0):
    """Poll pred() until truthy, or None once `timeout` passed. Running out
    of the RUN's budget raises BudgetExhausted instead: a leg that outruns
    budget() takes teardown down with it, and "the clock ran out" is not
    the same answer as "the product never did it"."""
    deadline = sm['clock']() + timeout
    while True:
        try:
            got = pred()
        except Exception:
            got = None
        if got:
            return got
        left = ctx['budget']()
        if left <= RESERVE_S:
            raise BudgetExhausted('%.0fs left, under the %.0fs teardown '
                                  'reserve' % (left, RESERVE_S))
        if sm['clock']() >= deadline:
            return None
        sm['sleep'](poll)


def live_port(ctx, sm, avoid=()):
    """A registered port that answers -- the only way to find Envoy again
    after a restart moved it."""
    for port in reg_ports(ctx['run_dir']):
        if port not in avoid and sm['alive'](port):
            return port
    return None


def settle(ctx, sm, st, timeout, avoid=()):
    """Wait for Envoy to answer again and point ctx's MCP calls at it."""
    port = until(ctx, sm, lambda: live_port(ctx, sm, avoid), timeout, 2.0)
    if port:
        st['port'] = port
        ctx['set_port'](port)
    return port


class Rec:
    """Step recorder for a leg's result dict. A fault stops at its first
    failed step: the injected state is already unknown, and stacking the
    next fault on it proves nothing."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.steps = []

    def ok(self, step, detail=''):
        return self.check(step, True, detail)

    def fail(self, step, detail):
        return self.check(step, False, detail)

    def check(self, step, cond, detail):
        ok = bool(cond)
        self.ctx['log']('%s%s: %s' % ('' if ok else 'FAIL ', step, detail))
        self.steps.append({'step': step, 'ok': ok, 'detail': detail})
        return ok
