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
being stopped is the one answering the MCP call; every corrupted file is
copied to <run_dir>/faults_backup first and put back when its step fails,
so the uninstall leg that follows finds the project as this leg did;
nothing outside run_dir is touched; run() reports, never raises.
"""

from __future__ import annotations

import os
import shutil

from . import _probe as P

_UP_S = 120.0        # ceiling for "Envoy is listening again"
_DOWN_S = 60.0       # ceiling for "the old socket is gone"
_REVIVE_S = 180.0    # watchdog: ~8s of dead ticks + an 18-frame Start

_GARBAGE = '{"instances": not-json,,, ]]] corrupted by the smoke faults leg\n'

# Log evidence, spelled as envoy_setup / embody_pyenv / EnvoyExt write it.
_FELL_BACK = 'using system Python for the MCP bridge'
_REPAIRED = 'Venv interpreter repaired'
_REVIVING = 'Watchdog: enabled but socket dead'


def _window(up_ms) -> str:
    """Envoyenable off, then back on after `up_ms` -- the only deterministic
    way to hold the port free for a fixed window. Stop() alone leaves
    Envoyenable True and the worker's exit hook auto-restarts ~1s later
    (EnvoyExt._onServerSuccess:7637), far too soon to bind under it. The
    second re-enable is a backstop for a dropped first; setting a par to the
    value it already holds fires no parexec."""
    on = "op.Embody.par.Envoyenable = True"
    return ("op.Embody.par.Envoyenable = False\n"
            "run(%r, fromOP=op.Embody, delayMilliSeconds=%d)\n"
            "run(%r, fromOP=op.Embody, delayMilliSeconds=%d)"
            % (on, int(up_ms), on, int(up_ms) + 45000))


def _watchdog_kill(port) -> str:
    """The wedge the watchdog exists for: the socket dies while Envoyenable
    stays ON and the status still claims 'Running on port N'.

    The competing recovery has to be pushed out of the way or it, not the
    watchdog, is what comes back: the worker's exit hook schedules an
    auto-restart ~1s later (EnvoyExt._onServerSuccess:7637). Stop() zeroes
    the restart window at its top, so the backoff counter is re-armed AFTER
    it -- the exit hook is dispatched on a later main-thread callback and
    cannot preempt this script -- which sends that restart to the 60s cap
    (_RESTART_BACKOFF_MAX) and leaves the ~8s watchdog as the only thing
    that can recover in between. _reviveDeadServer bumps the server
    generation, so the queued restart lands stale and no-ops. Envoyenable
    is never touched. The last run() is a safety net, so a watchdog that
    never fires still does not cost the uninstall leg its live Envoy."""
    stale = ("o = op.Embody\n"
             "o.store('envoy_running', True)\n"
             "o.par.Envoystatus = 'Running on port %d'" % int(port))
    net = ("o = op.Embody\n"
           "if not o.ext.Envoy._probeAlive():\n"
           "    o.store('envoy_running', False)\n"
           "    o.ext.Envoy.Start()")
    return ("import time\n"
            "o = op.Embody\n"
            "e = o.ext.Envoy\n"
            "e.Stop()\n"
            "e._restart_count = 7\n"
            "e._restart_window_start = time.time()\n"
            "e._last_start_time = time.time()\n"
            "run(%r, fromOP=o, delayMilliSeconds=5000)\n"
            "run(%r, fromOP=o, delayMilliSeconds=200000)" % (stale, net))


def _down(ctx, sm, port) -> bool:
    """The deferred kill actually closed the socket we were talking to."""
    return bool(P.until(ctx, sm, lambda: not sm['alive'](port), _DOWN_S))


def _confirm_target(ctx, rec) -> bool:
    """Refuse to inject anything until the instance on this port says its
    project.folder IS the run dir. A collided or stale port would otherwise
    carry every fault into whatever else is listening -- the developer's own
    session included (the same gate smoke_run.probe_mcp takes before it
    mutates anything)."""
    folder = str(ctx['py']('result = project.folder'))
    want = {os.path.normcase(os.path.abspath(ctx['run_dir'])),
            os.path.normcase(os.path.realpath(ctx['run_dir']))}
    got = {os.path.normcase(os.path.abspath(folder)),
           os.path.normcase(os.path.realpath(folder))}
    return rec.check('leg.target', bool(want & got),
                     'project.folder=%s' % folder)


def _same_path(a, b) -> bool:
    """Same existing directory, whatever the separators and case -- uv may
    rewrite pyvenv.cfg's home with a different spelling of one path."""
    if not a or not os.path.isdir(a):
        return False
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


def _check_running(ctx, rec, step, port) -> bool:
    """Envoy says Running on the port the leg is talking to, and answers."""
    status = str(ctx['py']("result = op.Embody.par.Envoystatus.eval()"))
    if not status.startswith('Running on port'):
        return rec.fail(step, 'Envoystatus is %r' % status)
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

    def bail(step, detail):        # put the interpreter back before failing
        P.write(cfg, before)
        return rec.fail(step, detail)

    entries = P.entries(site)
    for path in (cfg, os.path.join(run_dir, '.mcp.json')):
        if os.path.isfile(path):
            P.backup(run_dir, path)
    fell, fixed = (P.log_count(run_dir, _FELL_BACK),
                   P.log_count(run_dir, _REPAIRED))
    P.write(cfg, broken)
    rec.ok('venv.inject',
           'home -> missing TD; %d site-packages entries' % len(entries))
    # The probe caches per venv path and the repair is capped at one attempt
    # per TD process; both must be cleared or the restart reuses the healthy
    # answer taken at boot (envoy_setup.py:144, _beginAsyncVenvRepair).
    ctx['py']("import sys\n"
              "op.Embody.ext.Envoy._venv_probe_ok = ''\n"
              "sys._embody_venv_repair_state = {}\n"
              "sys._embody_venv_repair_results = {}\nresult = 'cleared'")
    # Restarting through the master switch, not a bare Stop(): Stop() alone
    # leaves the exit hook to auto-restart ~1s later (EnvoyExt.py:7637), a
    # window too short to observe the socket ever having closed.
    P.defer(ctx, _window(15000))
    if not _down(ctx, sm, old):
        return bail('venv.restart', 'port %d never closed' % old)
    if not P.settle(ctx, sm, st, _UP_S):
        return bail('venv.restart', 'Envoy never came back')
    rec.ok('venv.restart', 'stopped, then answering on %d' % st['port'])
    if not P.until(ctx, sm,
                   lambda: P.log_count(run_dir, _FELL_BACK) > fell, 60.0, 2.0):
        return bail('venv.fallback',
                    'no system-Python fallback logged (envoy_setup.py:167)')
    rec.ok('venv.fallback', 'logged the fallback to system Python')
    if not P.until(ctx, sm,
                   lambda: P.log_count(run_dir, _REPAIRED) > fixed,
                   _UP_S, 2.0):
        return bail('venv.repair', 'no success logged (embody_pyenv.py:1046)')
    back = P.until(ctx, sm, lambda: P.is_venv_command(P.mcp_command(run_dir),
                                                      run_dir), _UP_S, 2.0)
    state = str(ctx['py']("import sys\nresult = sorted(getattr("
                          "sys, '_embody_venv_repair_state', {}).values())"))
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
            return False
    return _check_running(ctx, rec, 'venv.running', st['port'])


# --- 2. the port Envoy wants is occupied ---

def _fault_port(ctx, rec, sm, st) -> bool:
    old = st['port']
    P.defer(ctx, _window(15000))
    # Bind-retry rather than free-then-bind: the gap between a free check
    # and the bind is exactly where Envoy could take the port back.
    held = P.until(ctx, sm, lambda: sm['hold'](old), _DOWN_S)
    if held is None:
        return rec.fail('port.hold', 'port %d never came free' % old)
    rec.ok('port.hold', 'the leg holds a listening socket on %d' % old)
    try:
        port = P.settle(ctx, sm, st, _UP_S, avoid=(old,))
        if not port:
            return rec.fail('port.fallback',
                            'never came back while %d was held' % old)
        if not rec.check('port.fallback', port != old,
                         'moved %d -> %d (_findAvailablePort)' % (old, port)):
            return False
        return _check_running(ctx, rec, 'port.running', port)
    finally:
        try:
            held.close()
        except OSError:
            pass


# --- 3. dead socket, Envoyenable still on -> the liveness watchdog ---

def _fault_watchdog(ctx, rec, sm, st) -> bool:
    run_dir, old = ctx['run_dir'], st['port']
    seen = P.log_count(run_dir, _REVIVING)
    P.defer(ctx, _watchdog_kill(old))
    if not _down(ctx, sm, old):
        return rec.fail('watchdog.kill', 'port %d never closed' % old)
    rec.ok('watchdog.kill', "socket %d closed, status left stale at "
                            "'Running on port %d'" % (old, old))
    port = P.settle(ctx, sm, st, _REVIVE_S)
    if not port:
        return rec.fail('watchdog.revive',
                        'nothing answered within %.0fs' % _REVIVE_S)
    said = P.until(ctx, sm,
                   lambda: P.log_count(run_dir, _REVIVING) > seen, 30.0, 2.0)
    on = str(ctx['py']("result = int(op.Embody.par.Envoyenable.eval())"))
    for step, good, detail in (
            ('watchdog.revive', said,
             'revived on %d, and the log says the watchdog did it' % port),
            ('watchdog.enable_flag', on.strip() == '1',
             'Envoyenable=%s throughout' % on.strip())):
        if not rec.check(step, good, detail):
            return False
    return _check_running(ctx, rec, 'watchdog.running', port)


# --- 4. corrupt instance registry ---

def _fault_registry(ctx, rec, sm, st) -> bool:
    run_dir, old = ctx['run_dir'], st['port']
    path = os.path.join(run_dir, '.embody', 'envoy.json')
    if not os.path.isfile(path):
        return rec.fail('registry.inject', 'no %s' % path)
    keep = P.backup(run_dir, path)
    # Corrupt it only once the server is DOWN: a live Stop() reads the file
    # on its way out, and any healthy rewrite before Start would leave
    # nothing for write_envoy_config to prove.
    P.defer(ctx, _window(15000))
    if not _down(ctx, sm, old):
        return rec.fail('registry.inject', 'port %d never closed' % old)
    P.write(path, _GARBAGE)
    if P.registry(run_dir) is not None:
        return rec.fail('registry.inject', 'the garbage still parses')
    rec.ok('registry.inject', 'envoy.json replaced with non-JSON while down')
    port = P.until(ctx, sm, lambda: P.live_port(ctx, sm), _UP_S, 2.0)
    if port is None:
        shutil.copyfile(keep, path)
        return rec.fail('registry.rewrite', 'still invalid, or no live '
                        'instance in it (write_envoy_config:1410)')
    st['port'] = port
    ctx['set_port'](port)
    rows = P.instances(run_dir)
    pids = [info.get('td_pid') for info in rows.values()]
    if not rec.check('registry.rewrite',
                     not ctx['pid'] or ctx['pid'] in pids,
                     'valid again: %d instance(s), port %d, pids %s'
                     % (len(rows), port, pids)):
        return False
    return _check_running(ctx, rec, 'registry.running', port)


def run(ctx):
    rec, sm = P.Rec(ctx), P.seams(ctx)
    st = {'port': int(ctx['port'])}
    error, ok = '', False
    try:
        if not _confirm_target(ctx, rec):
            return {'ok': False, 'steps': rec.steps,
                    'error': 'the port does not answer for the run dir -- '
                             'nothing was injected'}
        ok = (_fault_venv(ctx, rec, sm, st)
              and _fault_port(ctx, rec, sm, st)
              and _fault_watchdog(ctx, rec, sm, st)
              and _fault_registry(ctx, rec, sm, st))
    except Exception as e:      # the contract: a leg reports, never raises
        error = '%s: %s' % (type(e).__name__, e)
    # However that went, the uninstall leg and the orchestrator's teardown
    # both need a live Envoy on a known port.
    try:
        if P.live_port(ctx, sm) is None:
            left = max(0.0, ctx['budget']() - P.RESERVE_S)
            ok = rec.check('leg.envoy_up',
                           P.settle(ctx, sm, st, min(_REVIVE_S, left)),
                           'Envoy reachable on %d' % st['port']) and ok
    except Exception as e:
        ok = False
        error = (error + '; ' if error else '') + 'leg.envoy_up %s' % e
    return {'ok': bool(ok) and not error, 'steps': rec.steps, 'error': error}
