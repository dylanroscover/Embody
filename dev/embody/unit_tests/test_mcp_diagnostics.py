"""
Test suite: MCP diagnostics and introspection handlers in ClaudiusExt.

Tests _get_td_info, _get_op_errors, _exec_op_method,
_get_td_classes, _get_td_class_details, _get_module_help.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPDiagnostics(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _get_td_info ---

    def test_get_td_info(self):
        result = self.claudius._get_td_info()
        self.assertDictHasKey(result, 'version')

    def test_get_td_info_has_os(self):
        result = self.claudius._get_td_info()
        self.assertDictHasKey(result, 'osName')

    # --- _get_op_errors ---

    def test_get_op_errors_clean_op(self):
        comp = self.sandbox.create(baseCOMP, 'clean_comp')
        result = self.claudius._get_op_errors(
            op_path=comp.path, recurse=False)
        self.assertDictHasKey(result, 'errorCount')
        self.assertEqual(result['errorCount'], 0)

    def test_get_op_errors_recursive(self):
        comp = self.sandbox.create(baseCOMP, 'parent_comp')
        comp.create(baseCOMP, 'child_comp')
        result = self.claudius._get_op_errors(
            op_path=comp.path, recurse=True)
        self.assertDictHasKey(result, 'errorCount')

    def test_get_op_errors_nonexistent(self):
        result = self.claudius._get_op_errors(
            op_path='/nonexistent', recurse=False)
        self.assertDictHasKey(result, 'error')

    # --- _exec_op_method ---

    def test_exec_op_method_cook(self):
        comp = self.sandbox.create(baseCOMP, 'method_test')
        result = self.claudius._exec_op_method(
            op_path=comp.path, method='cook', args=[], kwargs={'force': True})
        self.assertNotIn('error', result)

    def test_exec_op_method_nonexistent_method(self):
        comp = self.sandbox.create(baseCOMP, 'bad_method')
        result = self.claudius._exec_op_method(
            op_path=comp.path, method='nonExistentMethod123')
        self.assertDictHasKey(result, 'error')

    # --- _get_td_classes ---

    def test_get_td_classes(self):
        result = self.claudius._get_td_classes()
        self.assertDictHasKey(result, 'classes')
        self.assertGreater(len(result['classes']), 0)

    # --- _get_td_class_details ---

    def test_get_td_class_details_op(self):
        result = self.claudius._get_td_class_details(class_name='OP')
        self.assertDictHasKey(result, 'methods')

    def test_get_td_class_details_nonexistent(self):
        result = self.claudius._get_td_class_details(
            class_name='NonExistentClass12345')
        self.assertDictHasKey(result, 'error')

    # --- _get_module_help ---

    def test_get_module_help_td_attr(self):
        # Use 'OP' to test the hasattr(td, name) path — fast unlike 'td' (7s)
        result = self.claudius._get_module_help(module_name='OP')
        self.assertDictHasKey(result, 'helpText')
        self.assertIn('OP', result['helpText'])

    def test_get_module_help_tdu(self):
        result = self.claudius._get_module_help(module_name='td.tdu')
        self.assertDictHasKey(result, 'helpText')
