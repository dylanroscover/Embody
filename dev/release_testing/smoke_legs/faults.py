"""
Break four things Embody claims to self-heal in the freshly installed smoke
project and prove each recovery from primary evidence -- the files on disk,
the run's own logs, a live tool call.

    1 venv interpreter that no longer runs -> system-Python fallback plus
      the in-place repair (envoy_setup.configure_mcp_client:162 ->
      EnvoyExt._beginAsyncVenvRepair -> embody_pyenv.repair_venv_interpreter)
    2 the port Envoy wants is occupied -> EnvoyExt._findAvailablePort
    3 dead socket while Envoyenable stays ON -> EnvoyExt._watchdogTick ->
      _reviveDeadServer
    4 corrupt instance registry -> envoy_setup.write_envoy_config

Invariants: every kill is DEFERRED through TD's run(), because the server
being stopped is the one answering the MCP call; every deferred timer is
generation-guarded through COMP storage ('_smoke_gen') and the generation
is bumped once more before run() returns, so nothing this leg armed can
fire into a later fault or into the uninstall leg (field 2026-09-19: a
stale re-enable from fault 1 started Envoy inside fault 4); every corrupted
file is copied to <run_dir>/faults_backup and written back the moment a
step fails; nothing outside run_dir is touched; run() reports, never raises.
"""

from __future__ import annotations

import json
import os

from . import _probe as P

_UP_S = 120.0        # ceiling for "Envoy is listening again"
_DOWN_S = 60.0       # ceiling for "the old socket is gone"
_REVIVE_S = 180.0    # watchdog: ~8s of dead ticks + an 18-frame Start
_MIN_S = 300.0       # under this much run budget, break nothing

_GARBAGE = '{"instances": not-json,,, ]]] corrupted by the smoke faults leg\n'

# Log evidence, spelled as envoy_setup / embody_pyenv / EnvoyExt write it.
# _FELL_BACK matches BOTH probe branches; only the 'broken' verdict
# (_BROKEN) starts a repair, so a probe that timed out on a slow runner
# reads as inconclusive instead of as a missing fallback
# (envoy_setup.py:166-178, embody_pyenv.probe_venv_python:954).
_FELL_BACK = 'using system Python'
_BROKEN = 'does not run ('
_REPAIRED = 'Venv interpreter repaired'
# _reviveDeadServer's own line (EnvoyExt.py:6687). It is watchdog-only:
# nothing but _watchdogTick calls it (EnvoyExt.py:6575, 6593), so it
# proves the WATCHDOG revived the socket and not _scheduleRestart's
# backoff -- while surviving which branch noticed. Pinning the
# socket-dead branch alone went red on 2026-09-20: _scheduleRestart
# rewrites Envoystatus ~1s after Stop(), so the tick found a stuck
# status rather than a stale 'Running on port N' one.
_REVIVING = 'unreachable while enabled'
# One file per window generation; the re-enables append to it.
_WINDOW_CRUMB = 'faults_window_%d.json'
_BRANCHES = (('socket dead', 'Watchdog: enabled but socket dead'),
             ('status stuck', 'Watchdog: status stuck at'))

# The probe caches per venv path and the repair is capped at one attempt per
# TD process; both have to go or the restart reuses the healthy answer taken
# at boot (envoy_setup.py:144, EnvoyExt._beginAsyncVenvRepair). Cleared in
# place, never rebound: _pollVenvRepair's worker closed over these dicts and
# a new one would strand it rescheduling forever (EnvoyExt.py:6063-6081).
_CLEAR_CACHES = ("import sys\n"
                 "op.Embody.ext.Envoy._venv_probe_ok = ''\n"
                 "for _n in ('_embody_venv_repair_state',\n"
                 "           '_embody_venv_repair_results'):\n"
                 "    _d = getattr(sys, _n, None)\n"
                 "    if isinstance(_d, dict):\n"
                 "        _d.clear()\n"
                 "result = 'cleared'")

_REPAIR_STATE = ("import sys\nresult = sorted(getattr("
                 "sys, '_embody_venv_repair_state', {}).values())")

# Fault 3 writes through private names and one storage key. Read them back
# first: a bare assignment to a renamed attribute is a silent no-op, and a
# renamed storage key would leave the watchdog reviving an ordinary dead
# socket while the leg claims it built the stale-status wedge.
_WEDGE_READY = (
    "e = op.Embody.ext.Envoy\n"
    "miss = [n for n in ('_restart_count', '_restart_window_start',\n"
    "                    '_last_start_time', 'Stop')\n"
    "        if not hasattr(e, n)]\n"
    "flag = op.Embody.fetch('envoy_running', False)\n"
    "if miss:\n"
    "    result = 'no attribute %s' % miss\n"
    "elif not flag:\n"
    "    result = 'envoy_running is %r' % (flag,)\n"
    "else:\n"
    "    result = 'ready'")


def _bump(st) -> int:
    """The generation a deferred script is armed with. Anything scheduled
    under an older one no-ops, so a timer that drifts past its fault cannot
    act on the next one."""
    st['gen'] += 1
    return st['gen']


def _window(up_ms, gen, run_dir='') -> str:
    """Envoyenable off, then back on after `up_ms` -- the only deterministic
    way to hold the port free for a fixed window. Stop() alone leaves
    Envoyenable True and the worker's exit hook auto-restarts ~1s later
    (EnvoyExt._onServerSuccess:7637), far too soon to bind under it.

    The re-enable is armed TWICE, on the wall clock and on frames: one
    missed dispatch left Envoy down for the rest of the leg (live
    2026-09-20, stop at frame 3634 with no re-enable after it), and with
    the socket gone there is no MCP call left to rescue it. Both are
    generation-guarded and skip a par that is already on, so whichever
    fires second is a no-op. Each writes a breadcrumb naming what it saw,
    so a window that never reopens says why instead of just timing out.
    """
    crumb = os.path.join(run_dir or '.', _WINDOW_CRUMB % int(gen))
    on = ("o = op.Embody\n"
          "g = o.fetch('_smoke_gen', 0)\n"
          "was = int(o.par.Envoyenable.eval())\n"
          "if g == %d and not was:\n"
          "    o.par.Envoyenable = True\n"
          "try:\n"
          "    import json as _j\n"
          "    with open(%r, 'a') as _f:\n"
          "        _j.dump({'by': %%r, 'gen': g, 'want': %d, 'was': was}, _f)\n"
          "        _f.write('\\n')\n"
          "except Exception:\n"
          "    pass" % (int(gen), crumb, int(gen)))
    frames = max(1, int(round(int(up_ms) / 1000.0 * 60)))
    return ("o = op.Embody\n"
            "o.store('_smoke_gen', %d)\n"
            "o.par.Envoyenable = False\n"
            "run(%r, fromOP=o, delayMilliSeconds=%d, wallTime=True)\n"
            "run(%r, fromOP=o, delayFrames=%d)"
            % (int(gen), on % 'clock', int(up_ms), on % 'frames', frames))


def _why_shut(run_dir, gen) -> str:
    """Why the window never reopened, in one clause: which re-enable
    dispatched, and what its guard saw. No crumb at all means TD never ran
    either callback -- the failure mode that once read as a product bug."""
    crumbs = _window_crumbs(run_dir, gen)
    if not crumbs:
        return ('neither re-enable dispatched (no breadcrumb) -- TD ran no '
                'run() callback for generation %d' % int(gen))
    return '; '.join('%s saw gen=%s enable=%s'
                     % (c.get('by'), c.get('gen'), c.get('was'))
                     for c in crumbs)


def _window_crumbs(run_dir, gen) -> list:
    """What the armed re-enables reported, oldest first."""
    out = []
    try:
        for line in P.read(os.path.join(run_dir,
                                        _WINDOW_CRUMB % int(gen))).splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _watchdog_kill(port, gen) -> str:
    """The wedge the watchdog exists for: the socket dies while Envoyenable
    stays ON and the status still claims 'Running on port N'. One script, so
    the wedge cannot lose the race to the ~8s revive. Re-arming the backoff
    AFTER Stop() (which zeroes the window at its top) sends the exit hook's
    auto-restart to the 60s cap and leaves the watchdog as the only
    recovery (EnvoyExt._onServerSuccess:7637, _scheduleRestart)."""
    return ("import time\n"
            "o = op.Embody\n"
            "o.store('_smoke_gen', %d)\n"
            "e = o.ext.Envoy\n"
            "e.Stop()\n"
            "e._restart_count = 7\n"
            "e._restart_window_start = time.time()\n"
            "e._last_start_time = time.time()\n"
            "o.store('envoy_running', True)\n"
            "o.par.Envoystatus = 'Running on port %d'"
            % (int(gen), int(port)))


def _restore(st) -> None:
    """Put every file this leg corrupted back. The uninstall leg that
    follows, and the run dir kept for triage, must both see the project as
    this leg found it."""
    for path, text in tuple(st.get('restore') or ()):
        try:
            P.write(path, text)
        except OSError:
            pass
    st['restore'] = []


def _spoil(st, run_dir, path) -> bool:
    """Back `path` up on disk and register its text for _restore."""
    if not os.path.isfile(path):
        return False
    P.backup(run_dir, path)
    st['restore'].append((path, P.read(path)))
    return True


def _down(ctx, sm, port) -> bool:
    """The deferred kill actually closed the socket we were talking to."""
    return bool(P.until(ctx, sm, lambda: not sm['alive'](port), _DOWN_S))


def _confirm_target(ctx, rec, quiet=False) -> bool:
    """Refuse to inject anything until the instance on this port says its
    project.folder IS the run dir -- a collided or stale port would carry
    every fault into whatever else is listening, the developer's own session
    included (the gate smoke_run.probe_mcp takes before it mutates
    anything). Re-taken after every restart, since each one can land on a
    port another TD registered; a re-check is silent unless it fails."""
    folder = str(ctx['py']('result = project.folder'))
    want = {os.path.normcase(os.path.abspath(ctx['run_dir'])),
            os.path.normcase(os.path.realpath(ctx['run_dir']))}
    got = {os.path.normcase(os.path.abspath(folder)),
           os.path.normcase(os.path.realpath(folder))}
    ok = bool(want & got)
    if ok and quiet:
        return True
    return rec.check('leg.target', ok, 'project.folder=%s' % folder)


def _settled(ctx, rec, sm, st, timeout, avoid=()):
    """Envoy answers again AND is still the smoke instance. None otherwise:
    the caller reports its own step, the gate failure is already recorded."""
    port = P.settle(ctx, sm, st, timeout, avoid)
    if port and not _confirm_target(ctx, rec, quiet=True):
        return None
    return port


def _same_path(a, b) -> bool:
    """Same existing directory, whatever the spelling -- uv may rewrite
    pyvenv.cfg's home with different separators, and a case-insensitive
    volume (Windows, default APFS) accepts either case. samefile is the
    only answer that holds on every platform: normcase folds case ONLY on
    Windows, so a re-cased home compared unequal on macOS (CI
    2026-09-20). normcase stays as the fallback for a home that no longer
    exists to stat."""
    if not a or not os.path.isdir(a):
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))


def _check_running(ctx, rec, step, port) -> bool:
    """Envoy says Running on the port the leg is talking to, and answers.
    The port in the status has to BE that port: a status naming a dead port
    is the 6.36/6.37 wedge, not a recovery."""
    status = str(ctx['py'](
        "result = op.Embody.par.Envoystatus.eval()")).strip()
    if not status.startswith('Running on port'):
        return rec.fail(step, 'Envoystatus is %r' % status)
    if status.split()[-1] != str(int(port)):
        return rec.fail(step, 'Envoystatus %r does not name port %d'
                        % (status, port))
    try:
        info = ctx['call']('get_td_info', {})
    except Exception as e:
        return rec.fail(step, 'tool call on port %d failed: %s' % (port, e))
    return rec.check(step, isinstance(info, dict) and info,
                     '%s; get_td_info answered on %d' % (status, port))


# --- 1. broken venv interpreter ---

def _fault_venv(ctx, rec, sm, st) -> bool:
    run_dir, old = ctx['run_dir'], st['port']
    cfg, site = P.venv_paths(run_dir)
    if not os.path.isfile(cfg):
        return rec.fail('venv.inject', 'no %s' % cfg)
    before = P.read(cfg)
    home = P.home_of(before)
    broken, hit = P.break_home(
        before, os.path.join(run_dir, 'no-such-touchdesigner', 'bin'))
    if not hit:
        return rec.fail('venv.inject', 'pyvenv.cfg carries no home line')
    entries = P.entries(site)
    if not entries:
        # Without this the no-deletion proof below compares [] with [] and
        # goes green on a site-packages nobody found.
        return rec.fail('venv.inject', 'no site-packages at %s' % site)

    def bail(step, detail):        # every failure puts the project back
        _restore(st)
        return rec.fail(step, detail)

    for path in (cfg, os.path.join(run_dir, '.mcp.json')):
        _spoil(st, run_dir, path)
    fell, broke, fixed = (P.log_count(run_dir, _FELL_BACK),
                          P.log_count(run_dir, _BROKEN),
                          P.log_count(run_dir, _REPAIRED))
    P.write(cfg, broken)
    # Injectable only where the launcher reads `home`: the win32 trampoline
    # dies (exit 103), a posix symlink into TD's framework may still start.
    # Proven here, against this machine's own venv, before TD is touched.
    if sm['runs'](P.venv_python(run_dir)):
        _restore(st)
        return rec.ok('venv.skipped', 'the interpreter still starts with a '
                      'bogus home on %s -- not injectable here'
                      % ctx['platform'])
    rec.ok('venv.inject',
           'home -> missing TD; %d site-packages entries' % len(entries))
    ctx['py'](_CLEAR_CACHES)
    # Restarting through the master switch, not a bare Stop(): Stop() alone
    # leaves the exit hook to auto-restart ~1s later (EnvoyExt.py:7637), a
    # window too short to observe the socket ever having closed.
    P.defer(ctx, _window(15000, _bump(st), ctx['run_dir']))
    if not _down(ctx, sm, old):
        return bail('venv.restart', 'port %d never closed' % old)
    if not _settled(ctx, rec, sm, st, _UP_S):
        return bail('venv.restart', 'Envoy never came back for this '
                    'run dir: %s' % _why_shut(ctx['run_dir'], st['gen']))
    rec.ok('venv.restart', 'stopped, then answering on %d' % st['port'])
    if not P.until(ctx, sm,
                   lambda: P.log_count(run_dir, _FELL_BACK) > fell, 60.0, 2.0):
        return bail('venv.fallback',
                    'no system-Python fallback logged (envoy_setup.py:166)')
    if P.log_count(run_dir, _BROKEN) <= broke:
        # The probe timed out or was refused: the product falls back and
        # deliberately does NOT repair, so there is nothing to prove.
        _restore(st)
        return rec.ok('venv.skipped', 'fell back to system Python on an '
                      'inconclusive probe (no "does not run" verdict)')
    rec.ok('venv.fallback', 'logged the fallback to system Python')
    if not P.until(ctx, sm,
                   lambda: P.log_count(run_dir, _REPAIRED) > fixed,
                   _UP_S, 2.0):
        return bail('venv.repair', 'no success logged (embody_pyenv.py:1046)')
    back = P.until(ctx, sm, lambda: P.is_venv_command(P.mcp_command(run_dir),
                                                      run_dir), _UP_S, 2.0)
    state = str(ctx['py'](_REPAIR_STATE))
    now, healed = P.entries(site), P.home_of(P.read(cfg))
    # The invariant is that NOTHING was deleted: the running server is
    # importing from this site-packages. An addition is reported, not failed.
    lost = sorted(set(entries) - set(now))
    for step, good, detail in (
            ('venv.repair', 'repaired' in state, 'state=%s' % state),
            ('venv.mcp_json', back, 'command=%s' % P.mcp_command(run_dir)),
            ('venv.no_deletion', not lost, '%d -> %d site-packages entries, '
             'missing %s, added %s' % (len(entries), len(now), lost,
                                       sorted(set(now) - set(entries)))),
            ('venv.pyvenv_cfg', _same_path(healed, home),
             'home=%r (was %r)' % (healed, home))):
        if not rec.check(step, good, detail):
            _restore(st)
            return False
    if not _check_running(ctx, rec, 'venv.running', st['port']):
        _restore(st)
        return False
    st['restore'] = []
    return True


# --- 2. the port Envoy wants is occupied ---

def _fault_port(ctx, rec, sm, st) -> bool:
    old = st['port']
    P.defer(ctx, _window(15000, _bump(st), ctx['run_dir']))
    # Bind-retry rather than free-then-bind: the gap between a free check
    # and the bind is exactly where Envoy could take the port back.
    held = P.until(ctx, sm, lambda: sm['hold'](old), _DOWN_S)
    if held is None:
        return rec.fail('port.hold', 'port %d never came free' % old)
    rec.ok('port.hold', 'the leg holds a listening socket on %d' % old)
    try:
        # settle already refuses `old`, so reaching here IS the move; record
        # it, never re-assert it.
        port = _settled(ctx, rec, sm, st, _UP_S, avoid=(old,))
        if not port:
            return rec.fail('port.fallback',
                            'never came back for this run dir while %d was '
                            'held: %s' % (old, _why_shut(ctx['run_dir'],
                                                         st['gen'])))
        rec.ok('port.fallback',
               'moved %d -> %d (_findAvailablePort)' % (old, port))
        return _check_running(ctx, rec, 'port.running', port)
    finally:
        try:
            held.close()
        except OSError:
            pass


# --- 3. dead socket, Envoyenable still on -> the liveness watchdog ---

def _fault_watchdog(ctx, rec, sm, st) -> bool:
    run_dir, old = ctx['run_dir'], st['port']
    ready = str(ctx['py'](_WEDGE_READY))
    if 'ready' not in ready:
        return rec.fail('watchdog.kill', 'the wedge cannot be built: %s'
                        % ready.strip())
    seen = P.log_count(run_dir, _REVIVING)
    P.defer(ctx, _watchdog_kill(old, _bump(st)))
    if not _down(ctx, sm, old):
        return rec.fail('watchdog.kill', 'port %d never closed' % old)
    branch_before = {name: P.log_count(run_dir, needle)
                     for name, needle in _BRANCHES}
    rec.ok('watchdog.kill', "socket %d closed; the same script left the "
                            "status at 'Running on port %d'" % (old, old))
    port = _settled(ctx, rec, sm, st, _REVIVE_S)
    if not port:
        return rec.fail('watchdog.revive',
                        'nothing answered for this run dir '
                        'within %.0fs' % _REVIVE_S)
    said = P.until(ctx, sm,
                   lambda: P.log_count(run_dir, _REVIVING) > seen, 30.0, 2.0)
    fired = [name for name, needle in _BRANCHES
             if P.log_count(run_dir, needle) > branch_before[name]]
    on = str(ctx['py']("result = int(op.Embody.par.Envoyenable.eval())"))
    for step, good, detail in (
            ('watchdog.revive', said,
             'revived on %d by the watchdog (_reviveDeadServer logged '
             '%r); branch: %s' % (port, _REVIVING,
                                  ', '.join(fired) or 'none logged')),
            ('watchdog.enable_flag', on.strip() == '1',
             'Envoyenable=%s throughout' % on.strip())):
        if not rec.check(step, good, detail):
            return False
    return _check_running(ctx, rec, 'watchdog.running', port)


# --- 4. corrupt instance registry ---

def _fault_registry(ctx, rec, sm, st) -> bool:
    run_dir, old = ctx['run_dir'], st['port']
    path = os.path.join(run_dir, '.embody', 'envoy.json')
    if not _spoil(st, run_dir, path):
        return rec.fail('registry.inject', 'no %s' % path)
    # Corrupt it only once the server is DOWN: a live Stop() reads the file
    # on its way out, and any healthy rewrite before Start would leave
    # nothing for write_envoy_config to prove.
    P.defer(ctx, _window(15000, _bump(st), ctx['run_dir']))
    if not _down(ctx, sm, old):
        return rec.fail('registry.inject', 'port %d never closed' % old)
    P.write(path, _GARBAGE)
    if P.registry(run_dir) is not None:
        return rec.fail('registry.inject', 'the garbage still parses')
    rec.ok('registry.inject', 'envoy.json replaced with non-JSON while down')
    port = _settled(ctx, rec, sm, st, _UP_S)
    if port is None:
        _restore(st)
        return rec.fail('registry.rewrite', 'still invalid, or no live '
                        'instance in it (write_envoy_config:1410)')
    rows = P.instances(run_dir)
    pids = [row.get('td_pid') for row in rows.values()
            if isinstance(row, dict)]
    if not rec.check('registry.rewrite',
                     not ctx['pid'] or ctx['pid'] in pids,
                     'valid again: %d instance(s), port %d, pids %s'
                     % (len(rows), port, pids)):
        _restore(st)
        return False
    if not _check_running(ctx, rec, 'registry.running', port):
        _restore(st)
        return False
    st['restore'] = []
    return True


def run(ctx):
    rec, sm = P.Rec(ctx), P.seams(ctx)
    st = {'port': 0, 'gen': 0, 'restore': []}
    error, ok = '', False
    try:
        st['port'] = int(ctx['port'])
        left = ctx['budget']()
        if left < _MIN_S:
            return {'ok': False, 'steps': rec.steps,
                    'error': 'inconclusive: %.0fs of run budget left, under '
                             'the %.0fs this leg needs -- nothing was '
                             'injected' % (left, _MIN_S)}
        if not _confirm_target(ctx, rec):
            return {'ok': False, 'steps': rec.steps,
                    'error': 'the port does not answer for the run dir -- '
                             'nothing was injected'}
        ok = (_fault_venv(ctx, rec, sm, st)
              and _fault_port(ctx, rec, sm, st)
              and _fault_watchdog(ctx, rec, sm, st)
              and _fault_registry(ctx, rec, sm, st))
    except P.BudgetExhausted as e:
        # Out of clock is not a product regression: no failed proof step.
        _restore(st)
        ok = False
        error = ('inconclusive: out of run budget after %s (%s)'
                 % (rec.steps[-1]['step'] if rec.steps else 'start', e))
    except Exception as e:      # the contract: a leg reports, never raises
        _restore(st)
        ok = False
        error = '%s: %s' % (type(e).__name__, e)
    # However that went, the uninstall leg and the orchestrator's teardown
    # both need a live Envoy on a known port -- and nothing this leg armed
    # may still be pending when they get it.
    try:
        if P.live_port(ctx, sm) is None:
            left = max(0.0, ctx['budget']() - P.RESERVE_S)
            ok = rec.check('leg.envoy_up',
                           P.settle(ctx, sm, st, min(_REVIVE_S, left)),
                           'Envoy reachable on %d' % st['port']) and ok
        if P.live_port(ctx, sm) is not None:
            ctx['py']("op.Embody.store('_smoke_gen', %d)" % _bump(st))
            rec.ok('leg.port', 'left on %d; the orchestrator was handed %d'
                   % (st['port'], int(ctx['port'] or 0)))
    except Exception as e:
        ok = False
        error = (error + '; ' if error else '') + 'leg.envoy_up %s' % e
    return {'ok': bool(ok) and not error, 'steps': rec.steps, 'error': error}
