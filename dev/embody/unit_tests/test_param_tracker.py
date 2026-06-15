"""
Test suite: ParameterTracker class in EmbodyExt.

Tests captureParameters, compareParameters, updateParamStore, removeComp.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestParameterTracker(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tracker = self.embody_ext.param_tracker

    # --- captureParameters ---

    def test_captureParameters_returns_dict(self):
        comp = self.sandbox.create(baseCOMP, 'capture_test')
        result = self.tracker.captureParameters(comp)
        self.assertIsInstance(result, dict)

    def test_captureParameters_has_params(self):
        comp = self.sandbox.create(baseCOMP, 'has_params')
        result = self.tracker.captureParameters(comp)
        self.assertGreater(len(result), 0)

    def test_captureParameters_excludes_externaltox(self):
        comp = self.sandbox.create(baseCOMP, 'no_externaltox')
        result = self.tracker.captureParameters(comp)
        self.assertNotIn('externaltox', result)

    def test_captureParameters_entries_have_value_expr_mode(self):
        comp = self.sandbox.create(baseCOMP, 'structured')
        result = self.tracker.captureParameters(comp)
        for name, entry in result.items():
            self.assertDictHasKey(entry, 'value')
            self.assertDictHasKey(entry, 'expr')
            self.assertDictHasKey(entry, 'mode')

    # --- compareParameters ---

    def test_compareParameters_no_change(self):
        comp = self.sandbox.create(baseCOMP, 'no_change')
        self.tracker.updateParamStore(comp)
        self.assertFalse(self.tracker.compareParameters(comp))

    def test_compareParameters_after_change(self):
        comp = self.sandbox.create(geometryCOMP, 'will_change')
        self.tracker.updateParamStore(comp)
        comp.par.tx = 42
        self.assertTrue(self.tracker.compareParameters(comp))

    def test_compareParameters_first_call_returns_false(self):
        comp = self.sandbox.create(baseCOMP, 'first_call')
        # First call should store and return False (no prior state)
        path = comp.path
        self.tracker.param_store.pop(path, None)
        self.assertFalse(self.tracker.compareParameters(comp))

    # --- updateParamStore ---

    def test_updateParamStore_stores_comp(self):
        comp = self.sandbox.create(baseCOMP, 'store_test')
        self.tracker.updateParamStore(comp)
        self.assertIn(comp.path, self.tracker.param_store)

    # --- removeComp ---

    def test_removeComp_removes_entry(self):
        comp = self.sandbox.create(baseCOMP, 'remove_test')
        self.tracker.updateParamStore(comp)
        self.tracker.removeComp(comp.path)
        self.assertNotIn(comp.path, self.tracker.param_store)

    def test_removeComp_nonexistent_no_error(self):
        # Removing a path that doesn't exist should not raise
        self.tracker.removeComp('/nonexistent/comp/path')

    # --- bindExpr tracking ---

    def test_captureParameters_includes_bindExpr(self):
        comp = self.sandbox.create(baseCOMP, 'bindexpr_capture')
        result = self.tracker.captureParameters(comp)
        for name, entry in result.items():
            self.assertDictHasKey(entry, 'bindExpr')

    def test_compareParameters_detects_bindExpr_change(self):
        # Create two comps so we can bind one's param to the other
        source = self.sandbox.create(geometryCOMP, 'bind_source')
        target = self.sandbox.create(geometryCOMP, 'bind_target')
        # Set up a bind expression on target
        target.par.tx.bindExpr = f"op('{source.path}').par.tx"
        self.tracker.updateParamStore(target)
        # Change the bind expression to a different source
        target.par.tx.bindExpr = f"op('{source.path}').par.ty"
        self.assertTrue(self.tracker.compareParameters(target))

    # --- rename tracking ---

    def test_removeComp_clears_old_path_for_rename(self):
        comp = self.sandbox.create(baseCOMP, 'rename_old')
        old_path = comp.path
        self.tracker.updateParamStore(comp)
        self.assertIn(old_path, self.tracker.param_store)
        # Simulate rename cleanup: remove old path
        self.tracker.removeComp(old_path)
        self.assertNotIn(old_path, self.tracker.param_store)

    # --- issue #21: broken expressions must not crash captureParameters ---
    # Regression: prior to the fix, par.eval() inside captureParameters raised
    # td.tdError for any expression that can't statically evaluate (extension
    # promotions, op('./missing'), me.inputs[0] without input, palette clones).
    # Hitting one such param during ExternalizeProject or Update cascaded into
    # save crashes and 0-byte .toe files.

    def _makeCompWithBrokenExpr(self, name):
        comp = self.sandbox.create(baseCOMP, name)
        page = comp.appendCustomPage('Test')
        p = page.appendStr('Brokenexpr', label='Broken')[0]
        # Assigning par.expr automatically switches mode to EXPRESSION in TD.
        p.expr = "ext.NoSuchExt.NoSuchAttr"
        return comp

    def test_captureParameters_broken_expression_does_not_raise(self):
        comp = self._makeCompWithBrokenExpr('broken_expr')
        # Must not raise - pre-fix this threw td.tdError
        result = self.tracker.captureParameters(comp)
        self.assertIsInstance(result, dict)
        self.assertIn('Brokenexpr', result)

    def test_captureParameters_expression_stores_authored_expr_not_eval(self):
        # captureParameters records the AUTHORED state, never par.eval(). For an
        # EXPRESSION-mode par the captured 'value' is the expression text itself
        # -- which is exactly what an externalized .tox/.tdn serializes. This
        # also means a broken expression can never raise here (no eval), and a
        # time-varying expression never reports a changing value (see
        # test_compareParameters_ignores_dependency_value_change).
        comp = self._makeCompWithBrokenExpr('broken_value')
        result = self.tracker.captureParameters(comp)
        self.assertEqual(result['Brokenexpr']['value'], 'ext.NoSuchExt.NoSuchAttr')
        self.assertEqual(result['Brokenexpr']['expr'], 'ext.NoSuchExt.NoSuchAttr')

    def test_updateParamStore_does_not_raise_on_broken_expression(self):
        comp = self._makeCompWithBrokenExpr('broken_store')
        # Pre-fix this propagated the same td.tdError from captureParameters
        self.tracker.updateParamStore(comp)
        self.assertIn(comp.path, self.tracker.param_store)

    def test_compareParameters_stable_with_broken_expression(self):
        # Repeated captures on an unchanged broken-expr param must report
        # no change (otherwise every Update flags it as dirty forever).
        comp = self._makeCompWithBrokenExpr('broken_stable')
        self.tracker.updateParamStore(comp)
        self.assertFalse(self.tracker.compareParameters(comp))

    def test_compareParameters_detects_expr_change_when_broken(self):
        # Even when neither expression can eval, swapping one broken expr
        # for a different broken expr must be detected (expr identity).
        comp = self._makeCompWithBrokenExpr('broken_changes')
        self.tracker.updateParamStore(comp)
        comp.par.Brokenexpr.expr = "ext.AlsoNoSuchExt.OtherAttr"
        self.assertTrue(self.tracker.compareParameters(comp))

    def test_compareParameters_detects_mode_change_when_broken(self):
        comp = self._makeCompWithBrokenExpr('broken_mode')
        self.tracker.updateParamStore(comp)
        # Assigning .val auto-switches the param to CONSTANT mode in TD -
        # must be detected as a change.
        comp.par.Brokenexpr.val = 'now constant'
        self.assertTrue(self.tracker.compareParameters(comp))

    def test_compareParameters_ignores_dependency_value_change(self):
        # A dependency-driven change to an expression's EVALUATED value must
        # NOT mark the COMP dirty -- the externalized .tox/.tdn stores the
        # authored expression text, which is unchanged, so re-exporting would
        # produce a byte-identical file. The prior implementation captured
        # par.eval() here, so a parameter bound to a time-varying expression
        # (absTime.frame, an audio level, a moving CHOP) reported a different
        # value every Refresh and triggered perpetual false-dirty re-exports.
        source = self.sandbox.create(geometryCOMP, 'eval_source')
        target = self.sandbox.create(baseCOMP, 'eval_target')
        page = target.appendCustomPage('Test')
        p = page.appendFloat('Linked', label='Linked')[0]
        p.expr = f"op('{source.path}').par.tx"
        source.par.tx = 0
        self.tracker.updateParamStore(target)
        source.par.tx = 42  # dependency changes; authored expression does not
        self.assertFalse(self.tracker.compareParameters(target),
            "A dependency-driven evaluated-value change must NOT report dirty "
            "(the authored expression -- what gets serialized -- is unchanged)")
        # Sanity: editing the authored expression itself IS detected.
        target.par.Linked.expr = f"op('{source.path}').par.ty"
        self.assertTrue(self.tracker.compareParameters(target),
            "Editing the authored expression must still be detected as dirty")
