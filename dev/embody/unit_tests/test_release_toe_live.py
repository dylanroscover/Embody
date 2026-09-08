"""In-TD tests for the release .toe export's READ side.

The export itself can never run under the test runner -- it destroys the
Embody COMP and quits the instance -- so this suite pins what the fake-TD
tests in test_release_toe.py only model: _release_collect against real
operators, the shell census on real .tdxn files, and that PreviewReleaseToe
touches nothing. Every path is read-only against the live project.
"""
import os
import re
import shutil
import tempfile

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestReleaseToePreviewLive(EmbodyTestCase):
    """PreviewReleaseToe and its collector against the live project."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='release_toe_live_')
        self._save = os.path.join(self._tmp, 'Release.toe').replace('\\', '/')
        # A probe folder INSIDE project.folder: buildAbsolutePath resolves
        # relative rows against it, and the census reads through that.
        self._probe_rel = '_release_toe_live_probe'
        self._probe_dir = os.path.join(project.folder, self._probe_rel)
        os.makedirs(self._probe_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._probe_dir, ignore_errors=True)
        super().tearDown()

    def _admin(self):
        return self.embody.op('embody_admin').module

    def _snapshot(self):
        e = self.embody
        return (str(e.par.Tdxnmode.eval()), bool(e.par.Tdxnstriponsave.eval()),
                str(e.par.Filecleanup.eval()), str(e.par.Status.eval()),
                e.fetch('_init_complete', None, search=False),
                e.fetch('_suppress_dialogs', None, search=False),
                bool(self.embody_ext.Externalizations.par.syncfile.eval()),
                bool(e.op('execute').par.active.eval()),
                self.embody_ext.Externalizations.numRows)

    def test_the_collector_reads_the_live_project(self):
        st = self._admin()._release_collect(
            self.embody_ext, 'pre_release_toe', self._save)
        self.assertEqual(st['embody_path'], self.embody.path)
        self.assertEqual(st['table_path'],
                         self.embody_ext.Externalizations.path)
        self.assertEqual(st['project_folder'], str(project.folder))
        self.assertTrue(st['save_dir_exists'])
        self.assertFalse(st['save_path_exists'])
        self.assertTrue(st['tracked_paths'])
        self.assertTrue(st['ops'])
        for comp in st['tdxn_comps']:
            self.assertIn('file_ops', comp, comp)
        keys = set(st['storage_keys'])
        self.assertIn('_tdn_rel_path', keys)
        self.assertIn('embed_dats_in_tdn', keys)
        self.assertNotIn('hover', keys)
        self.assertNotIn('test_results', keys)
        prefix = self.embody.path + '/'
        for ref in st['refs']:
            self.assertFalse(ref['path'].startswith(prefix), ref)
        for line in st['op_errors']:
            self.assertFalse(line.startswith(prefix), line)
        for dat in st['execute_dats']:
            self.assertFalse(dat['path'].startswith('/local/'), dat)

    def test_the_preview_changes_nothing(self):
        before = self._snapshot()
        plan = self.embody.PreviewReleaseToe(save_path=self._save)
        self.assertEqual(plan['save_path'], self._save)
        for key in ('readiness', 'inline', 'scrub', 'footprint', 'references',
                    'disarm', 'privacy', 'presave_hooks'):
            self.assertIn(key, plan)
        self.assertEqual([e['path'] for e in plan['footprint']][0],
                         self.embody.path)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(os.path.exists(self._save))

    def test_the_preview_refuses_the_running_project_and_bad_paths(self):
        toe = self.embody_ext._resolveProjectToe()
        if not toe:
            self.skipTest('project has no .toe on disk')
        refusals = self.embody.PreviewReleaseToe(
            save_path=toe)['readiness']['refusals']
        self.assertTrue(any('running project itself' in r for r in refusals),
                        refusals)
        base = re.sub(r'\.\d+$', '', os.path.basename(toe)[:-4]) + '.toe'
        refusals = self.embody.PreviewReleaseToe(
            save_path=base)['readiness']['refusals']   # relative: lands beside
        self.assertTrue(any('save series' in r or 'running project itself' in r
                            for r in refusals), refusals)
        refusals = self.embody.PreviewReleaseToe(
            save_path='C:/definitely/not/here/x.toe')['readiness']['refusals']
        self.assertTrue(any('save folder does not exist' in r
                            for r in refusals), refusals)
        refusals = self.embody.PreviewReleaseToe(
            save_path=self._save, privacy_key='k')['readiness']['refusals']
        if not licenses.isPro:
            self.assertTrue(any('Pro licence' in r for r in refusals), refusals)

    def test_the_shell_census_reads_real_tdxn_files(self):
        empty = os.path.join(self._probe_dir, 'empty.tdxn')
        full = os.path.join(self._probe_dir, 'full.tdxn')
        with open(empty, 'w', encoding='utf-8') as fh:
            fh.write("version: '2.1'\nroot: empty\noperators: []\n")
        with open(full, 'w', encoding='utf-8') as fh:
            fh.write("version: '2.1'\nroot: full\noperators:\n"
                     "  - name: a\n    type: baseCOMP\n"
                     "  - name: b\n    type: nullTOP\n")
        ops = self._admin()._release_tdxn_file_ops
        self.assertEqual(ops(self.embody_ext, self._probe_rel + '/empty.tdxn'), 0)
        self.assertEqual(ops(self.embody_ext, self._probe_rel + '/full.tdxn'), 2)
        self.assertIsNone(ops(self.embody_ext, self._probe_rel + '/missing.tdxn'))
        gate = self._admin().plan_release_readiness(
            tdxn_comps=[{'path': '/x/empty', 'present': True, 'children': 0,
                         'file_ops': 0},
                        {'path': '/x/full', 'present': True, 'children': 0,
                         'file_ops': 2}],
            frame=absTime.frame, op_errors=[], perform_mode=False,
            project_saved=True, save_path=self._save, save_dir_exists=True)
        self.assertEqual(gate['shells'], ['/x/full'])
