"""
Test suite: MCP operator management handlers in ClaudiusExt.

Tests _create_operator, _delete_operator, _get_operator, _copy_operator,
_rename_operator, _query_network, _find_children, _cook_operator.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPOperators(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _create_operator ---

    def test_create_operator_valid(self):
        result = self.claudius._create_operator(
            parent_path=self.sandbox.path, op_type='baseCOMP', name='test_comp')
        self.assertDictHasKey(result, 'path')
        self.assertIn('test_comp', result['path'])

    def test_create_operator_without_name(self):
        result = self.claudius._create_operator(
            parent_path=self.sandbox.path, op_type='textDAT')
        self.assertDictHasKey(result, 'path')

    def test_create_operator_invalid_parent(self):
        result = self.claudius._create_operator(
            parent_path='/nonexistent/path', op_type='baseCOMP')
        self.assertDictHasKey(result, 'error')

    def test_create_operator_in_non_comp(self):
        dat = self.sandbox.create(textDAT, 'not_a_comp')
        result = self.claudius._create_operator(
            parent_path=dat.path, op_type='baseCOMP')
        self.assertDictHasKey(result, 'error')

    # --- _delete_operator ---

    def test_delete_operator_existing(self):
        comp = self.sandbox.create(baseCOMP, 'to_delete')
        result = self.claudius._delete_operator(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_delete_operator_nonexistent(self):
        result = self.claudius._delete_operator(op_path='/nonexistent/op')
        self.assertDictHasKey(result, 'error')

    # --- _get_operator ---

    def test_get_operator_returns_info(self):
        comp = self.sandbox.create(baseCOMP, 'info_target')
        result = self.claudius._get_operator(op_path=comp.path)
        self.assertEqual(result['name'], 'info_target')
        self.assertDictHasKey(result, 'type')
        self.assertDictHasKey(result, 'family')

    def test_get_operator_has_parameters(self):
        comp = self.sandbox.create(baseCOMP, 'par_target')
        result = self.claudius._get_operator(op_path=comp.path)
        self.assertDictHasKey(result, 'parameters')

    def test_get_operator_nonexistent(self):
        result = self.claudius._get_operator(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _copy_operator ---

    def test_copy_operator_basic(self):
        source = self.sandbox.create(baseCOMP, 'source')
        result = self.claudius._copy_operator(
            source_path=source.path, dest_parent=self.sandbox.path, new_name='copy')
        self.assertDictHasKey(result, 'new_path')
        self.assertIn('copy', result['new_path'])

    def test_copy_operator_without_name(self):
        source = self.sandbox.create(baseCOMP, 'source2')
        result = self.claudius._copy_operator(
            source_path=source.path, dest_parent=self.sandbox.path)
        self.assertDictHasKey(result, 'new_path')

    # --- _rename_operator ---

    def test_rename_operator(self):
        comp = self.sandbox.create(baseCOMP, 'old_name')
        result = self.claudius._rename_operator(
            op_path=comp.path, new_name='new_name')
        self.assertTrue(result.get('success'))

    def test_rename_operator_nonexistent(self):
        result = self.claudius._rename_operator(
            op_path='/nonexistent', new_name='whatever')
        self.assertDictHasKey(result, 'error')

    # --- _query_network ---

    def test_query_network_basic(self):
        self.sandbox.create(baseCOMP, 'child1')
        self.sandbox.create(textDAT, 'child2')
        result = self.claudius._query_network(parent_path=self.sandbox.path)
        self.assertDictHasKey(result, 'operators')
        self.assertGreaterEqual(len(result['operators']), 2)

    def test_query_network_with_type_filter(self):
        self.sandbox.create(baseCOMP, 'comp_child')
        self.sandbox.create(textDAT, 'dat_child')
        result = self.claudius._query_network(
            parent_path=self.sandbox.path, op_type='baseCOMP')
        for op_info in result['operators']:
            self.assertEqual(op_info['type'], 'baseCOMP')

    def test_query_network_nonexistent(self):
        result = self.claudius._query_network(parent_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _find_children ---

    def test_find_children_by_name(self):
        self.sandbox.create(baseCOMP, 'target_comp')
        self.sandbox.create(textDAT, 'other_dat')
        result = self.claudius._find_children(
            op_path=self.sandbox.path, name='target*')
        self.assertDictHasKey(result, 'operators')
        names = [o['name'] for o in result['operators']]
        self.assertIn('target_comp', names)

    def test_find_children_by_type(self):
        self.sandbox.create(baseCOMP, 'a_comp')
        self.sandbox.create(textDAT, 'a_dat')
        result = self.claudius._find_children(
            op_path=self.sandbox.path, type='textDAT')
        for o in result['operators']:
            self.assertEqual(o['type'], 'textDAT')

    # --- _cook_operator ---

    def test_cook_operator(self):
        comp = self.sandbox.create(baseCOMP, 'cookable')
        result = self.claudius._cook_operator(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_cook_operator_nonexistent(self):
        result = self.claudius._cook_operator(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')
