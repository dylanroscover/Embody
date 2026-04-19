"""
Test suite: MCP batch_operations handler in EnvoyExt.

Tests _batch_operations with success, error, and edge cases.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPBatch(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- Success cases ---

    def test_batch_single_operation(self):
        result = self.envoy._batch_operations(operations=[
            {'tool': 'get_td_info', 'params': {}},
        ])
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(len(result['results']), 1)

    def test_batch_multiple_operations(self):
        result = self.envoy._batch_operations(operations=[
            {'tool': 'get_td_info', 'params': {}},
            {'tool': 'execute_python', 'params': {'code': 'result = 42'}},
        ])
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['results'][1]['result'], '42')

    def test_batch_empty_list(self):
        result = self.envoy._batch_operations(operations=[])
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(len(result['results']), 0)

    # --- Error handling ---

    def test_batch_stops_on_error(self):
        result = self.envoy._batch_operations(operations=[
            {'tool': 'get_td_info', 'params': {}},
            {'tool': 'get_op', 'params': {'op_path': '/nonexistent_op_xyz'}},
            {'tool': 'get_td_info', 'params': {}},
        ])
        self.assertFalse(result['success'])
        # Should have stopped at index 1, never reaching index 2
        self.assertEqual(result['count'], 2)

    def test_batch_invalid_spec(self):
        result = self.envoy._batch_operations(operations=[
            {'tool': 'get_td_info', 'params': {}},
            'not_a_dict',
        ])
        self.assertFalse(result['success'])
        self.assertEqual(result['count'], 2)
        self.assertIn('error', result['results'][1])

    def test_batch_missing_tool_key(self):
        result = self.envoy._batch_operations(operations=[
            {'params': {'op_path': '/'}},
        ])
        self.assertFalse(result['success'])
        self.assertIn('error', result['results'][0])

    def test_batch_not_a_list(self):
        result = self.envoy._batch_operations(operations='not_a_list')
        self.assertIn('error', result)

    def test_batch_nested_not_allowed(self):
        result = self.envoy._batch_operations(operations=[
            {'tool': 'batch_operations', 'params': {'operations': []}},
        ])
        self.assertFalse(result['success'])
        self.assertIn('error', result['results'][0])

    # --- Practical patterns ---

    def test_batch_create_and_query(self):
        """Create an op then query it in one batch."""
        parent = self.sandbox.path
        result = self.envoy._batch_operations(operations=[
            {'tool': 'create_op', 'params': {
                'parent_path': parent, 'op_type': 'nullTOP', 'name': 'batch_test_null'}},
            {'tool': 'get_op', 'params': {
                'op_path': f'{parent}/batch_test_null'}},
        ])
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 2)
        self.assertIn('batch_test_null', result['results'][0].get('path', ''))
