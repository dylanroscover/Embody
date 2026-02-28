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
