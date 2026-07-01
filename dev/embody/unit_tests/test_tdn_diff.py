"""Integration tests for the diff_tdn Envoy handler (_diff_tdn).

Exercises the full main-thread chain -- path resolution, non-interactive live
export, on-disk read, normalize, diff, envelope -- against real TDN-externalized
sandbox COMPs. Extensions are referenced inline (no caching); self.embody_ext is
the runner's re-resolving property.
"""

import os

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestDiffTdnHandler(EmbodyTestCase):

    def _make_tdn_comp(self, name):
        """Create a sandbox baseCOMP with a child + custom par, externalize it
        as TDN, and return (comp, rel_path). Caller must _cleanup()."""
        comp = self.sandbox.create(baseCOMP, name)
        child = comp.create(constantCHOP, 'c')
        child.par.value0 = 1.0
        pg = comp.appendCustomPage('Test')
        p = pg.appendFloat('Testval', label='Test Value')[0]
        p.default = 0.0
        p.val = 0.0
        self.embody_ext.applyTagToOperator(comp, self.embody.par.Tdntag.eval())
        rel = None
        tbl = self.embody_ext.Externalizations
        for r in range(1, tbl.numRows):
            if tbl[r, 'path'].val == comp.path:
                rel = tbl[r, 'rel_file_path'].val
                break
        return comp, child, rel

    def _cleanup(self, comp, rel):
        if rel:
            try:
                p = str(self.embody_ext.buildAbsolutePath(
                    self.embody_ext.normalizePath(rel)))
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
            try:
                self.embody_ext.RemoveListerRow(comp.path, rel, delete_file=True)
            except Exception:
                pass
        try:
            self.embody_ext._tdn_fingerprints.pop(comp.path, None)
        except Exception:
            pass

    def test_error_when_not_tdn_externalized(self):
        comp = self.sandbox.create(baseCOMP, 'not_tdn')
        res = op.Embody.ext.Envoy._diff_tdn(comp.path)
        self.assertIn('error', res)
        self.assertIn('not TDN-externalized', res['error'])

    def test_error_when_op_missing(self):
        res = op.Embody.ext.Envoy._diff_tdn('/no/such/op')
        self.assertIn('error', res)

    def test_clean_right_after_externalize(self):
        comp, child, rel = self._make_tdn_comp('diff_clean')
        try:
            self.assertIsNotNone(rel, 'COMP should be TDN-externalized')
            res = op.Embody.ext.Envoy._diff_tdn(comp.path)
            self.assertNotIn('error', res)
            self.assertEqual(res['baseline'], 'disk')
            self.assertFalse(
                res['changed'],
                'a freshly externalized COMP must diff clean (live == disk); '
                'got: %s' % res.get('counts'))
        finally:
            self._cleanup(comp, rel)

    def test_detects_child_param_change(self):
        comp, child, rel = self._make_tdn_comp('diff_param')
        try:
            self.assertIsNotNone(rel)
            # Mutate live state away from the on-disk .tdn.
            child.par.value0 = 9.0
            res = op.Embody.ext.Envoy._diff_tdn(comp.path)
            self.assertNotIn('error', res)
            self.assertTrue(res['changed'],
                            'a live param edit must show as changed')
            paths = [m['path'] for m in res['modified']]
            self.assertTrue(any(p.endswith('/c') for p in paths),
                            'changed child "c" must appear in modified: %s' % paths)
        finally:
            self._cleanup(comp, rel)

    def test_detects_root_param_change(self):
        comp, child, rel = self._make_tdn_comp('diff_root')
        try:
            self.assertIsNotNone(rel)
            comp.par.Testval = 5.0  # root COMP's own custom par
            res = op.Embody.ext.Envoy._diff_tdn(comp.path)
            self.assertTrue(res['changed'])
            kinds = [m['kind'] for m in res['modified']]
            self.assertIn('root', kinds,
                          'root COMP own-par change must surface as kind=root')
        finally:
            self._cleanup(comp, rel)

    def test_envelope_shape(self):
        comp, child, rel = self._make_tdn_comp('diff_shape')
        try:
            res = op.Embody.ext.Envoy._diff_tdn(comp.path)
            for key in ('schema_version', 'baseline', 'comp_path', 'file',
                        'changed', 'counts', 'added', 'removed', 'modified',
                        'truncated', 'warnings'):
                self.assertIn(key, res, 'envelope missing %r' % key)
            self.assertEqual(res['file_exists'], True)
        finally:
            self._cleanup(comp, rel)

    def test_status_recommends_diff_tdn(self):
        comp, child, rel = self._make_tdn_comp('diff_hint')
        try:
            status = op.Embody.ext.Envoy._get_externalization_status(comp.path)
            self.assertEqual(status.get('strategy'), 'tdn')
            self.assertEqual(status.get('recommended_tool'), 'diff_tdn')
            self.assertIn('absolute_path', status)
        finally:
            self._cleanup(comp, rel)

    def test_resolves_tdn_filename_to_comp(self):
        # diff_tdn must accept a .tdn filename, not just a COMP path.
        comp, child, rel = self._make_tdn_comp('diff_byname')
        try:
            self.assertIsNotNone(rel)
            fname = rel.replace('\\', '/').rsplit('/', 1)[-1]  # bare filename
            res = op.Embody.ext.Envoy._diff_tdn(fname)
            self.assertNotIn('error', res)
            self.assertEqual(res['comp_path'], comp.path)
        finally:
            self._cleanup(comp, rel)

    def test_resolves_tdn_relpath_to_comp(self):
        comp, child, rel = self._make_tdn_comp('diff_byrel')
        try:
            self.assertIsNotNone(rel)
            res = op.Embody.ext.Envoy._diff_tdn(rel)  # full repo-relative path
            self.assertNotIn('error', res)
            self.assertEqual(res['comp_path'], comp.path)
        finally:
            self._cleanup(comp, rel)

    def test_project_wide_summary(self):
        # Project-wide: every live TDN COMP, summarized. A high cap is passed so
        # the freshly-added probe is always examined regardless of how many
        # other TDN COMPs the project already has (the default cap could
        # otherwise truncate before reaching it).
        comp, child, rel = self._make_tdn_comp('diff_proj')
        try:
            self.assertIsNotNone(rel)
            child.par.value0 = 9.0  # make it unsaved-dirty vs disk
            res = op.Embody.ext.TDN.DiffAllLiveVsDisk(max_comps=100000)
            self.assertEqual(res.get('scope'), 'project')
            for key in ('changed_count', 'clean_count', 'skipped_count',
                        'changed', 'skipped', 'truncated'):
                self.assertIn(key, res, 'project envelope missing %r' % key)
            changed_paths = [e['comp_path'] for e in res['changed']]
            self.assertIn(comp.path, changed_paths,
                          'changed COMP must appear in project-wide summary')
        finally:
            self._cleanup(comp, rel)

    def test_project_wide_handler_routes_empty_target(self):
        # The handler maps an empty target to the project-wide summary.
        res = op.Embody.ext.Envoy._diff_tdn('')
        self.assertNotIn('error', res)
        self.assertEqual(res.get('scope'), 'project')
