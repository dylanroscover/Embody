"""
Test suite: TDN dirty detection via network fingerprint.

Regression test for the bug where _computeTDNFingerprint captured only
structural/visual properties (name, type, position, color, tags, flags,
comment, connections, annotations) and IGNORED parameter values -- so a
parameter edit on a TDN-strategy COMP, whether on the COMP's own top-level
custom pars or on a child operator, did not change the fingerprint and was
never flagged dirty. Only structural/layout edits were detected.

These tests pin the fix: parameter edits (constant, expression) on the root
and on children must change the fingerprint, structural edits must still
change it, and Embody-managed About metadata (Build/Date/Touchbuild) must
NOT change it (so build bumps don't cause spurious dirty).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNFingerprint(EmbodyTestCase):

    def _make_comp(self):
        """A base COMP with one child and a top-level custom float par."""
        comp = self.sandbox.create(baseCOMP, 'fp_comp')
        child = comp.create(constantCHOP, 'child1')
        child.nodeX, child.nodeY = 0, 0
        pg = comp.appendCustomPage('Test')
        p = pg.appendFloat('Testval', label='Test Value')[0]
        p.default = 0.0
        p.val = 0.0
        return comp, child

    def _fp(self, comp):
        return self.embody_ext._computeTDNFingerprint(comp)

    # --- parameter changes (the regression) ---

    def test_top_level_param_change_detected(self):
        comp, _ = self._make_comp()
        before = self._fp(comp)
        comp.par.Testval = 5.0
        self.assertNotEqual(
            before, self._fp(comp),
            'Top-level parameter change must change the TDN fingerprint')

    def test_child_param_change_detected(self):
        comp, child = self._make_comp()
        before = self._fp(comp)
        child.par.value0 = 3.0
        self.assertNotEqual(
            before, self._fp(comp),
            'Child operator parameter change must change the TDN fingerprint')

    def test_expression_change_detected(self):
        comp, _ = self._make_comp()
        comp.par.Testval = 1.0
        before = self._fp(comp)
        comp.par.Testval.expr = 'absTime.frame'
        self.assertNotEqual(
            before, self._fp(comp),
            'Switching a par to expression mode must change the fingerprint')

    # --- structural changes still detected ---

    def test_structural_move_still_detected(self):
        comp, child = self._make_comp()
        before = self._fp(comp)
        child.nodeX += 100
        self.assertNotEqual(
            before, self._fp(comp),
            'Structural (move) change must still change the fingerprint')

    # --- About-page metadata excluded (no spurious dirty on build bumps) ---

    def test_build_par_bump_ignored(self):
        comp, _ = self._make_comp()
        ab = comp.appendCustomPage('About')
        bp = ab.appendInt('Build')[0]
        bp.default = 1
        bp.val = 1
        before = self._fp(comp)
        comp.par.Build = 99
        self.assertEqual(
            before, self._fp(comp),
            'Build/Date/Touchbuild bumps must NOT change the fingerprint')

    # --- determinism ---

    def test_fingerprint_stable_when_unchanged(self):
        comp, _ = self._make_comp()
        self.assertEqual(
            self._fp(comp), self._fp(comp),
            'Fingerprint must be deterministic for an unchanged network')

    # --- baseline primed at externalize time (no lazy-on-first-scan gap) ---

    def test_baseline_primed_on_externalize(self):
        """TDN externalization must prime the dirty-detection baseline
        immediately -- not lazily on the first _isTDNDirty scan. Otherwise a
        param edit landing between externalize and that first scan would be
        absorbed into the baseline and the COMP would wrongly read clean."""
        import os
        emb = self.embody_ext
        comp = self.sandbox.create(baseCOMP, 'fp_prime')
        comp.create(constantCHOP, 'c')
        rel = None
        try:
            emb.applyTagToOperator(comp, self.embody.par.Tdntag.eval())
            tbl = emb.Externalizations
            for r in range(1, tbl.numRows):
                if tbl[r, 'path'].val == comp.path:
                    rel = tbl[r, 'rel_file_path'].val
                    break
            # Baseline must exist right after externalize, with NO scan between.
            self.assertIn(
                comp.path, emb._tdn_fingerprints,
                'externalization must prime the TDN fingerprint baseline')
            self.assertFalse(
                emb._isTDNDirty(comp),
                'a freshly externalized COMP must read clean')
        finally:
            if rel:
                try:
                    p = str(emb.buildAbsolutePath(emb.normalizePath(rel)))
                    if os.path.isfile(p):
                        os.remove(p)
                except Exception:
                    pass
                try:
                    emb.RemoveListerRow(comp.path, rel, delete_file=True)
                except Exception:
                    pass
            emb._tdn_fingerprints.pop(comp.path, None)
            try:
                emb.param_tracker.removeComp(comp)
            except Exception:
                pass
