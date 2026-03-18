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
        self.assertApproxEqual(comp.par.tx.eval(), 10.0)

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
            op_path=comp.path, par_name='tx')
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

    # --- Toggle parameters ---

    def test_set_toggle_parameter_with_0_1(self):
        comp = self.sandbox.create(geometryCOMP, 'toggle_test')
        result = self.envoy._set_parameter(
            op_path=comp.path, par_name='render', value='1')
        self.assertTrue(result.get('success'))
