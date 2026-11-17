"""
Smoke leg `wizard`: a first-time drag-in, clicked through the REAL setup
wizard in a virgin TD of its own, on the build under test.

The project already holds two COMPs carrying the External .tox link TD writes
on drag-in. v6.3.4 opened the 'Dropped .tox Expression Detected' dialog before
the wizard, the first thing a new user met (field 2026-11-16, a tutorial).
Since v6.3.5 the dialog waits while the wizard is pending and the question is
a wizard step. _wizard_td.py (staged as smoke_bootstrap.py) answers Unlink
with Envoy, git, externalize and Convoy off; this module judges the
wizard_check.json it writes.

It boots ctx['build'] even when the smoke TD started on an older release,
never touches the smoke TD (so its failure skips no other leg), and deletes
nothing. The screenshot it leaves (wizard_toxdrop.png) is for a human.
"""

from __future__ import annotations

import json
import os

FLAG = 'wizard_check.json'
_TD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '_wizard_td.py')
_COMPS = ('DROSTE', 'TDTodo')   # _wizard_td.USERS
_TIMEOUT_S = 180.0              # TD boot + init + 8 wizard steps, generously


def judge(res):
    """[{'step', 'ok', 'detail'}] for one wizard_check.json."""
    steps = []

    def step(name, ok, detail=''):
        steps.append({'step': name, 'ok': bool(ok), 'detail': detail})

    step('the TD side ran', not res.get('error'), res.get('error') or '')
    step('no dialog before or during the wizard', 'dialog_frame' not in res,
         f"fired at frame {res['dialog_frame']}" if 'dialog_frame' in res
         else '')
    hint = res.get('toxdrop_hint') or ''
    step('the wizard asks about the links',
         'toxdrop' in (res.get('steps') or [])
         and all(c in hint for c in _COMPS),
         hint or f"steps {res.get('steps')}")
    step('keep is preselected', res.get('toxdrop_preselected') == 'ignore',
         str(res.get('toxdrop_preselected')))
    step('unlink clears every link',
         res.get('pref_after') == 'clean'
         and res.get('user_links_left') == []
         and res.get('embody_link_left') is False,
         f"pref {res.get('pref_after')}, still linked "
         f"{res.get('user_links_left')}, Embody "
         f"{res.get('embody_link_left')}")
    return steps


def run(ctx):
    try:
        if ctx['budget']() < _TIMEOUT_S:
            return {'ok': False, 'steps': [],
                    'error': f"{ctx['budget']():.0f}s left in the run, "
                             f"needs {_TIMEOUT_S:.0f}s"}
        out = ctx['fresh_td'](_TD_SCRIPT, FLAG, _TIMEOUT_S,
                              lambda t: bool(t.strip()))
        ctx['log'](f"wizard: run dir {out['dir']}")
        base = {'run_dir': out['dir'], 'teardown': out['teardown']}
        if not out['ok']:
            return dict(base, ok=False, steps=[],
                        error=f'no {FLAG} within {_TIMEOUT_S:.0f}s')
        res = json.loads(out['text'])
        steps = judge(res)
        steps.append({'step': 'its TD quit',
                      'ok': bool((out['teardown'] or {}).get('ok')),
                      'detail': (out['teardown'] or {}).get('message', '')})
        return dict(base, ok=all(s['ok'] for s in steps), steps=steps,
                    error='', version=res.get('version'))
    except Exception as e:  # a leg never raises
        return {'ok': False, 'steps': [],
                'error': f'{type(e).__name__}: {e}'}
