"""
Extra release-smoke legs, run by smoke_run.py after the fresh-install verdict
(ready PASS, features 7/7, MCP probe ok) and before teardown:

    python dev/release_testing/smoke_run.py --legs upgrade,faults,uninstall

Each leg is a module in this package exposing

    run(ctx) -> {'ok': bool, 'steps': [{'step', 'ok', 'detail'}], 'error': str}

and NEVER raises out of run(): a leg that cannot run returns ok=False with
the reason in 'error'. Legs run in the fixed order below whatever the
caller listed, because `uninstall` removes the Embody COMP (nothing can
follow it) and `upgrade` must see the freshly installed OLD build first.

`ctx` (all keys always present):
    run_dir      the run directory (project.folder of the smoke TD)
    repo         the checkout being released
    build        {'tox', 'version', 'sha256', 'size', 'td_build'} -- the NEW
                 build (the manifest asset), even when the smoke TD started on
                 an older one
    installed    {'tox', 'version'} -- what the smoke TD actually booted
                 (== build unless --upgrade-from staged an older tox)
    upgrade_from {'tox', 'version', 'tag'} or None -- the older build that was
                 staged for the upgrade leg
    port         the smoke Envoy's port (int)
    pid          the smoke TD's pid
    td_exe       the TouchDesigner executable / .app
    platform     sys.platform
    call(name, arguments, timeout=30) -> dict
                 one Envoy MCP tool call against the smoke instance; raises
                 RuntimeError with the tool's error text on failure
    py(code, timeout=30) -> object
                 execute_python on the smoke instance; returns the tool's
                 `result` value (a repr string as Envoy returns it)
    log(msg)     prints to the orchestrator's stderr with the leg's prefix
    budget()     seconds left in the run's overall ceiling
    wait_for_flag(name, timeout, done) -> (text, ok)
                 poll a file in run_dir (same helper the orchestrator uses)

A leg may restart the smoke TD's Envoy (the port may change: read it back
from `.embody/envoy.json` in run_dir or from Envoystatus) but must leave a
TD running at `pid` for the orchestrator's teardown -- except `uninstall`,
which may leave the project without Embody but must not quit TD.
"""

from __future__ import annotations

import importlib
import time

# Fixed execution order; also the set of valid names.
LEG_ORDER = ('upgrade', 'faults', 'uninstall')


def parse_legs(spec):
    """'upgrade,uninstall' -> ('upgrade', 'uninstall') in LEG_ORDER; raises
    ValueError on an unknown name so a typo never silently runs nothing."""
    wanted = {s.strip() for s in (spec or '').split(',') if s.strip()}
    unknown = wanted - set(LEG_ORDER)
    if unknown:
        raise ValueError(f'unknown smoke leg(s): {sorted(unknown)}; '
                         f'valid: {", ".join(LEG_ORDER)}')
    return tuple(leg for leg in LEG_ORDER if leg in wanted)


def run_legs(names, ctx, clock=None, registry=None):
    """Run the named legs in LEG_ORDER. Returns {name: result} plus
    '_ok' (all ran and passed) -- a leg that raises is recorded as a
    failure, never propagated, so teardown always happens. `registry`
    maps name -> run callable (tests inject fakes; default imports
    smoke_legs.<name>)."""
    clock = clock or time.monotonic
    out = {'_ok': True}
    for name in parse_legs(','.join(names) if not isinstance(names, str)
                           else names):
        t0 = clock()
        try:
            if registry and name in registry:
                fn = registry[name]
            else:
                fn = importlib.import_module(f'{__name__}.{name}').run
            res = fn(ctx)
            if not isinstance(res, dict):
                res = {'ok': False, 'steps': [],
                       'error': f'{name}.run returned {type(res).__name__}'}
        except Exception as e:  # a leg must never take the run down
            res = {'ok': False, 'steps': [],
                   'error': f'{name} raised {type(e).__name__}: {e}'}
        res.setdefault('ok', False)
        res.setdefault('steps', [])
        res.setdefault('error', '')
        res['elapsed_s'] = round(clock() - t0, 1)
        out[name] = res
        if not res['ok']:
            out['_ok'] = False
            if name != 'uninstall':
                # A failed upgrade or fault leg leaves the project in an
                # unknown state; uninstall would then test nothing real.
                break
    return out
