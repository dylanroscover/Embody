"""
Test suite: MCP parameter handlers in EnvoyExt.

Tests _set_parameter and _get_parameter with various modes.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPParameters(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- _set_parameter (constant) ---

    def test_set_parameter_constant(self):
        comp = self.sandbox.create(geometryCOMP, 'par_test')
        result = self.envoy._set_parameter(
            op_path=comp.path, par_name='tx', value='5')
        self.assertTrue(result.get('success'))

    def test_set_parameter_verifies_value(self):
        comp = self.sandbox.create(geometryCOMP, 'par_verify')
        self.envoy._set_parameter(
            op_path=comp.path, par_name='tx', value='10')
        self.assertAlmostEqual(comp.par.tx.eval(), 10.0)

    # --- _set_parameter (expression) ---

    def test_set_parameter_expression(self):
        comp = self.sandbox.create(geometryCOMP, 'expr_test')
        result = self.envoy._set_parameter(
            op_path=comp.path, par_name='tx', expr='absTime.seconds')
        self.assertTrue(result.get('success'))

    # --- _set_parameter (mode) ---

    def test_set_parameter_mode_constant(self):
        comp = self.sandbox.create(geometryCOMP, 'mode_test')
        result = self.envoy._set_parameter(
            op_path=comp.path, par_name='tx', value='7', mode='constant')
        self.assertTrue(result.get('success'))

    # --- _set_parameter errors ---

    def test_set_parameter_nonexistent_op(self):
        result = self.envoy._set_parameter(
            op_path='/nonexistent', par_name='tx', value='1')
        self.assertDictHasKey(result, 'error')

    def test_set_parameter_nonexistent_par(self):
        comp = self.sandbox.create(baseCOMP, 'bad_par')
        result = self.envoy._set_parameter(
            op_path=comp.path, par_name='nonexistent_param_xyz', value='1')
        self.assertDictHasKey(result, 'error')

    # --- _get_parameter ---

    def test_get_parameter_basic(self):
        comp = self.sandbox.create(geometryCOMP, 'get_par')
        result = self.envoy._get_parameter(
            op_path=comp.path, par_name='tx')
        self.assertDictHasKey(result, 'value')
        self.assertDictHasKey(result, 'mode')

    def test_get_parameter_has_metadata(self):
        comp = self.sandbox.create(geometryCOMP, 'meta_par')
        result = self.envoy._get_parameter(
            op_path=comp.path, par_name='tx', details=True)
        self.assertDictHasKey(result, 'label')
        self.assertDictHasKey(result, 'default')

    def test_get_parameter_nonexistent_op(self):
        result = self.envoy._get_parameter(
            op_path='/nonexistent', par_name='tx')
        self.assertDictHasKey(result, 'error')

    def test_get_parameter_nonexistent_par(self):
        comp = self.sandbox.create(baseCOMP, 'bad_get')
        result = self.envoy._get_parameter(
            op_path=comp.path, par_name='nonexistent_param_xyz')
        self.assertDictHasKey(result, 'error')

    def test_get_parameter_bind_mode(self):
        # bindMaster is a Par, which has no .path (issue 201)
        src = self.sandbox.create(geometryCOMP, 'bind_src')
        dst = self.sandbox.create(geometryCOMP, 'bind_dst')
        dst.par.tx.bindExpr = "op('bind_src').par.tx"
        result = self.envoy._get_parameter(op_path=dst.path, par_name='tx')
        self.assertNotIn('error', result)
        self.assertEqual(result['bindExpr'], "op('bind_src').par.tx")
        self.assertEqual(result['bindMaster'], src.path)
        self.assertEqual(result['bindMasterPar'], 'tx')

    def test_get_parameter_broken_bind(self):
        dst = self.sandbox.create(geometryCOMP, 'bind_broken')
        dst.par.tx.bindExpr = "op('missing_src').par.tx"
        result = self.envoy._get_parameter(op_path=dst.path, par_name='tx')
        self.assertNotIn('error', result)
        self.assertIsNone(result['bindMaster'])

    def test_get_parameter_unevaluable_expression(self):
        # a failing eval must not hide the mode and expression (issue 201)
        comp = self.sandbox.create(geometryCOMP, 'bad_expr')
        comp.par.tx.expr = "op('does_not_exist').par.tx + 1"
        result = self.envoy._get_parameter(op_path=comp.path, par_name='tx')
        self.assertNotIn('error', result)
        self.assertEqual(result['expression'], "op('does_not_exist').par.tx + 1")
        self.assertStartsWith(result['value'], '<eval error:')

    # an export only applies on a later frame, so the source naming is
    # tested directly: a Channel's or Cell's bool() and str() are its value

    def test_export_source_names_channel_at_zero(self):
        chop = self.sandbox.create(constantCHOP, 'exp_chop')
        chop.par.name0 = 'geo1:tx'
        chop.par.value0 = 0
        self.assertEqual(
            self.envoy._exportSourceName(chop['geo1:tx']), 'geo1:tx')

    def test_export_source_names_cell(self):
        table = self.sandbox.create(tableDAT, 'exp_table')
        table.clear()
        table.appendRow(['path', 'parameter', 'value'])
        table.appendRow(['geo1', 'tx', '0'])
        self.assertEqual(self.envoy._exportSourceName(table[1, 2]), '[1,2]')

    # --- Toggle parameters ---

    def test_set_toggle_parameter_with_0_1(self):
        comp = self.sandbox.create(geometryCOMP, 'toggle_test')
        result = self.envoy._set_parameter(
            op_path=comp.path, par_name='render', value='1')
        self.assertTrue(result.get('success'))
