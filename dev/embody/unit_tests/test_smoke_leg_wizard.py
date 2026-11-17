"""
Test suite: the release smoke's `wizard` leg (smoke_legs/wizard.py, its TD
side smoke_legs/_wizard_td.py, and smoke_run.run_fresh_td).

Pure Python, TD-import-free. The verdicts are the real wizard_check.json
outputs of the first runs (2026-11-16): v6.3.4 showed the dropped-.tox
dialog before the wizard and must read red; v6.3.6 must read green. Pins:
- each claim the leg makes has a broken result that turns it red;
- run() never raises and refuses a budget too short to boot a TD;
- the second TD is staged with the leg's script as its bootstrap, isolated
  from Convoy, and quit even when the wait blows up;
- a failed wizard leg halts no other leg (it never touched the smoke TD);
- the dialog title and the clicked option names are the product's own.
Inside TD every test skips.
"""

import copy
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_IN_TD = 'td' in sys.modules

_DEV = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_RT = os.path.join(_DEV, 'release_testing')
_SRC = os.path.join(_DEV, 'embody', 'Embody')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if not _IN_TD:
    if _RT not in sys.path:
        sys.path.insert(0, _RT)
    legs = importlib.import_module('smoke_legs')
    wizard = importlib.import_module('smoke_legs.wizard')
    td_side = _load('wizard_td_under_test',
                    os.path.join(_RT, 'smoke_legs', '_wizard_td.py'))
    smoke = _load('smoke_run_wizard_under_test',
                  os.path.join(_RT, 'smoke_run.py'))

# v6.3.6, the fixed build (trimmed to what the leg reads)
GREEN = {
    'tox': 'Embody-v6.3.6.tox',
    'steps': ['mode', 'externalize', 'toxdrop', 'assistant', 'convoy',
              'git', 'summary'],
    'toxdrop_hint': '2 COMPs still carry the External .tox link '
                    'TouchDesigner adds on drag-in: DROSTE, TDTodo.',
    'toxdrop_preselected': 'ignore',
    'pref_after': 'clean', 'user_links_left': [], 'embody_link_left': False,
    'version': '6.3.6',
}
# v6.3.4: the dialog at frame 67, the wizard at 80, no step, links kept
RED = {
    'tox': 'Embody-v6.3.4.tox', 'dialog_frame': 67,
    'steps': ['mode', 'externalize', 'assistant', 'convoy', 'git',
              'summary'],
    'pref_after': 'ask', 'user_links_left': ['DROSTE', 'TDTodo'],
    'embody_link_left': False, 'version': '6.3.4',
}


class _Case(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only')

    def failed(self, res):
        return [s['step'] for s in wizard.judge(res) if not s['ok']]


class TestJudge(_Case):

    def test_the_fixed_build_passes_every_step(self):
        self.assertEqual(self.failed(GREEN), [])

    def test_the_dialog_before_the_wizard_reads_red(self):
        got = self.failed(RED)
        self.assertIn('no dialog before or during the wizard', got)
        self.assertIn('the wizard asks about the links', got)
        self.assertIn('unlink clears every link', got)

    def test_a_td_side_error_reads_red(self):
        self.assertIn('the TD side ran',
                      self.failed(dict(GREEN, error='wizard never opened')))

    def test_a_skipped_step_reads_red(self):
        res = copy.deepcopy(GREEN)
        res['steps'].remove('toxdrop')
        self.assertIn('the wizard asks about the links', self.failed(res))

    def test_a_hint_missing_a_comp_reads_red(self):
        res = dict(GREEN, toxdrop_hint='1 COMP still carries it: DROSTE.')
        self.assertIn('the wizard asks about the links', self.failed(res))

    def test_unlink_preselected_reads_red(self):
        self.assertIn('keep is preselected',
                      self.failed(dict(GREEN, toxdrop_preselected='clean')))

    def test_any_link_left_reads_red(self):
        for bad in ({'user_links_left': ['TDTodo']},
                    {'embody_link_left': True},
                    {'embody_link_left': None},
                    {'pref_after': 'ask'}):
            with self.subTest(bad=bad):
                self.assertIn('unlink clears every link',
                              self.failed(dict(GREEN, **bad)))


class _Ctx(dict):
    """The leg's view of the orchestrator; fresh_td is scripted."""

    def __init__(self, reply=None, budget=900.0, raises=None):
        super().__init__()
        self.calls = []

        def fresh_td(script, flag, timeout, done):
            self.calls.append((script, flag, timeout, done))
            if raises:
                raise raises
            return reply

        self.update({'budget': lambda: budget, 'fresh_td': fresh_td,
                     'log': lambda m: None})


def _reply(res=GREEN, ok=True, teardown_ok=True):
    return {'dir': '/tmp/embody-smoke/win32-x', 'ok': ok,
            'text': json.dumps(res) if res is not None else None,
            'teardown': {'ok': teardown_ok, 'message': 'exited gracefully'}}


class TestRun(_Case):

    def test_a_green_run_passes_and_reports_its_run_dir(self):
        ctx = _Ctx(_reply())
        out = wizard.run(ctx)
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['run_dir'], '/tmp/embody-smoke/win32-x')
        script, flag, _, done = ctx.calls[0]
        self.assertTrue(os.path.isfile(script), script)
        self.assertEqual(os.path.basename(script), '_wizard_td.py')
        self.assertEqual(flag, td_side.FLAG)
        self.assertFalse(done(''))
        self.assertTrue(done('{}'))

    def test_a_red_verdict_fails_the_leg(self):
        self.assertFalse(wizard.run(_Ctx(_reply(RED)))['ok'])

    def test_no_flag_fails_and_names_the_run_dir(self):
        out = wizard.run(_Ctx(_reply(None, ok=False)))
        self.assertFalse(out['ok'])
        self.assertIn(td_side.FLAG, out['error'])
        self.assertEqual(out['run_dir'], '/tmp/embody-smoke/win32-x')

    def test_a_td_left_running_fails(self):
        out = wizard.run(_Ctx(_reply(teardown_ok=False)))
        self.assertFalse(out['ok'])
        self.assertIn('its TD quit',
                      [s['step'] for s in out['steps'] if not s['ok']])

    def test_it_never_raises(self):
        out = wizard.run(_Ctx(raises=OSError('no TD')))
        self.assertFalse(out['ok'])
        self.assertIn('no TD', out['error'])

    def test_too_little_budget_refuses_before_launching(self):
        ctx = _Ctx(_reply(), budget=30.0)
        self.assertFalse(wizard.run(ctx)['ok'])
        self.assertEqual(ctx.calls, [])


class TestRunFreshTd(_Case):
    """smoke_run.run_fresh_td, with staging, launch, wait and quit faked."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix='embody_wizard_leg_test_')
        self.seen = {}
        self.saved = {n: getattr(smoke, n) for n in (
            'stage_run', 'launch_td', 'wait_for', 'quit_smoke_td')}

        class _Proc:
            def poll(self):
                return 0

        def stage_run(repo, build, out, **kw):
            self.seen['stage'] = (repo, out, kw)
            return {'dir': self.dir}

        def launch_td(td_exe, toe, console=None, env=None):
            self.seen['launch'] = (td_exe, toe, env)
            return 4242, _Proc()

        def quit_smoke_td(pid, run_dir, **kw):
            self.seen['quit'] = (pid, run_dir)
            return {'ok': True, 'message': 'closed'}

        smoke.stage_run = stage_run
        smoke.launch_td = launch_td
        smoke.quit_smoke_td = quit_smoke_td
        smoke.wait_for = lambda path, timeout, done: ('{"x": 1}', True)
        self.script = os.path.join(self.dir, 'leg_script.py')
        with open(self.script, 'w', encoding='utf-8') as f:
            f.write('# the leg script\n')

    def tearDown(self):
        for n, fn in self.saved.items():
            setattr(smoke, n, fn)
        super().tearDown()

    def _run(self):
        return smoke.run_fresh_td('/repo', {'tox': 'x.tox'}, '/out', 'td.exe',
                                  self.script, 'flag.json', 60, bool)

    def test_the_leg_script_becomes_the_bootstrap(self):
        out = self._run()
        with open(os.path.join(self.dir, 'smoke_bootstrap.py'),
                  encoding='utf-8') as f:
            self.assertEqual(f.read(), '# the leg script\n')
        self.assertEqual(out, {'dir': self.dir, 'text': '{"x": 1}',
                               'ok': True,
                               'teardown': {'ok': True, 'message': 'closed'}})
        self.assertEqual(self.seen['quit'], (4242, self.dir))

    def test_convoy_is_isolated_in_the_run_dir(self):
        self._run()
        self.assertEqual(self.seen['stage'][2], {'convoy_isolated': True})
        env = self.seen['launch'][2]
        self.assertEqual(env['EMBODY_CONVOY_DATA_DIR'],
                         os.path.join(self.dir, 'convoy-data'))
        self.assertTrue(os.path.isdir(env['EMBODY_CONVOY_DATA_DIR']))

    def test_its_td_is_quit_even_when_the_wait_raises(self):
        def boom(*a):
            raise KeyboardInterrupt
        smoke.wait_for = boom
        with self.assertRaises(KeyboardInterrupt):
            self._run()
        self.assertEqual(self.seen['quit'], (4242, self.dir))

    def test_the_context_stages_beside_the_smoke_run_capped_by_budget(self):
        got = {}
        saved = smoke.run_fresh_td
        smoke.run_fresh_td = lambda *a: got.setdefault('args', a)
        try:
            run_dir = os.path.join('/out', 'embody-smoke', 'win32-1')
            ctx = smoke.make_leg_context(
                {'dir': run_dir}, {'repo': '/repo', 'tox': 'x.tox'}, {}, None,
                9870, 1, 'td.exe', lambda: 50.0)
            ctx['fresh_td']('s.py', 'f.json', 180, bool)
        finally:
            smoke.run_fresh_td = saved
        repo, build, out, td_exe, script, flag, timeout, _ = got['args']
        self.assertEqual((repo, out, td_exe, script, flag),
                         ('/repo', '/out', 'td.exe', 's.py', 'f.json'))
        self.assertEqual(timeout, 50.0)


class TestLegOrder(_Case):

    def test_wizard_runs_first(self):
        self.assertEqual(legs.parse_legs('uninstall,wizard,upgrade'),
                         ('wizard', 'upgrade', 'uninstall'))

    def _ran(self, failing):
        ran = []

        def fake(name):
            def fn(ctx):
                ran.append(name)
                return {'ok': name != failing}
            return fn
        out = legs.run_legs(legs.LEG_ORDER, {},
                            registry={n: fake(n) for n in legs.LEG_ORDER})
        return ran, out

    def test_a_failed_wizard_leg_halts_nothing(self):
        ran, out = self._ran('wizard')
        self.assertEqual(ran, list(legs.LEG_ORDER))
        self.assertFalse(out['_ok'])

    def test_a_failed_upgrade_still_halts_the_rest(self):
        ran, _ = self._ran('upgrade')
        self.assertEqual(ran, ['wizard', 'upgrade'])


class TestNamesAreTheProducts(_Case):
    """A renamed dialog would leave the dialog check blind and the leg green
    forever; a renamed option would stall the wizard on its step."""

    def _read(self, *parts):
        with open(os.path.join(_SRC, *parts), encoding='utf-8') as f:
            return f.read()

    def test_the_dialog_title_is_embodys(self):
        self.assertIn(repr(td_side.DIALOG), self._read('EmbodyExt.py'))

    def test_every_clicked_step_and_option_exists(self):
        logic = self._read('wizard', 'logic.py')
        tdxn = self._read('wizard.tdxn')
        for step, option in td_side.CHOICES.items():
            with self.subTest(step=step):
                self.assertRegex(logic, r"'%s':\{'g':'(grp_%s)'"
                                 % (re.escape(step), re.escape(step)))
                self.assertIn(option, tdxn)

    def test_the_judged_comps_are_the_dropped_ones(self):
        self.assertEqual(wizard._COMPS, td_side.USERS)
        self.assertEqual(wizard.FLAG, td_side.FLAG)
