"""
Test suite: MCP diagnostics and introspection handlers in EnvoyExt.

Tests _get_td_info, _get_op_errors, _exec_op_method,
_get_td_classes, _get_td_class_details, _get_module_help, _get_logs.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPDiagnostics(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- _get_td_info ---

    def test_get_td_info(self):
        result = self.envoy._get_td_info()
        self.assertDictHasKey(result, 'version')

    def test_get_td_info_has_os(self):
        result = self.envoy._get_td_info()
        self.assertDictHasKey(result, 'osName')

    # --- _get_op_errors ---

    def test_get_op_errors_clean_op(self):
        comp = self.sandbox.create(baseCOMP, 'clean_comp')
        result = self.envoy._get_op_errors(
            op_path=comp.path, recurse=False)
        self.assertDictHasKey(result, 'errorCount')
        self.assertEqual(result['errorCount'], 0)

    def test_get_op_errors_recursive(self):
        comp = self.sandbox.create(baseCOMP, 'parent_comp')
        comp.create(baseCOMP, 'child_comp')
        result = self.envoy._get_op_errors(
            op_path=comp.path, recurse=True)
        self.assertDictHasKey(result, 'errorCount')

    def test_get_op_errors_nonexistent(self):
        result = self.envoy._get_op_errors(
            op_path='/nonexistent', recurse=False)
        self.assertDictHasKey(result, 'error')

    # --- _exec_op_method ---

    def test_exec_op_method_cook(self):
        comp = self.sandbox.create(baseCOMP, 'method_test')
        result = self.envoy._exec_op_method(
            op_path=comp.path, method='cook', args=[], kwargs={'force': True})
        self.assertNotIn('error', result)

    def test_exec_op_method_nonexistent_method(self):
        comp = self.sandbox.create(baseCOMP, 'bad_method')
        result = self.envoy._exec_op_method(
            op_path=comp.path, method='nonExistentMethod123')
        self.assertDictHasKey(result, 'error')

    # --- _get_td_classes ---

    def test_get_td_classes(self):
        result = self.envoy._get_td_classes()
        self.assertDictHasKey(result, 'classes')
        self.assertGreater(len(result['classes']), 0)

    # --- _get_td_class_details ---

    def test_get_td_class_details_op(self):
        result = self.envoy._get_td_class_details(class_name='OP')
        self.assertDictHasKey(result, 'methods')

    def test_get_td_class_details_nonexistent(self):
        result = self.envoy._get_td_class_details(
            class_name='NonExistentClass12345')
        self.assertDictHasKey(result, 'error')

    # --- _get_module_help ---

    def test_get_module_help_td_attr(self):
        # Use 'OP' to test the hasattr(td, name) path - fast unlike 'td' (7s)
        result = self.envoy._get_module_help(module_name='OP')
        self.assertDictHasKey(result, 'helpText')
        self.assertIn('OP', result['helpText'])

    def test_get_module_help_tdu(self):
        result = self.envoy._get_module_help(module_name='td.tdu')
        self.assertDictHasKey(result, 'helpText')

    # --- _get_logs ---

    def test_get_logs_returns_entries(self):
        op.Embody.Log('test_get_logs marker', 'INFO')
        result = self.envoy._get_logs()
        self.assertDictHasKey(result, 'entries')
        self.assertIsInstance(result['entries'], list)
        self.assertDictHasKey(result, 'latest_id')
        self.assertDictHasKey(result, 'total_in_buffer')

    def test_get_logs_count_capped(self):
        result = self.envoy._get_logs(count=5)
        self.assertLessEqual(len(result['entries']), 5)

    def test_get_logs_level_filter(self):
        op.Embody.Log('an error for the filter test', 'ERROR')
        result = self.envoy._get_logs(level='ERROR')
        for e in result['entries']:
            self.assertEqual(e['level'], 'ERROR')

    def test_get_logs_since_id_returns_only_newer(self):
        op.Embody.Log('before the cursor', 'INFO')
        cursor = self.envoy._get_logs()['latest_id']
        op.Embody.Log('after the cursor', 'INFO')
        result = self.envoy._get_logs(since_id=cursor)
        for e in result['entries']:
            self.assertGreater(e['id'], cursor)
