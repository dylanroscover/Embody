"""
Test suite: MCP operator management handlers in ClaudiusExt.

Tests _create_op, _delete_op, _get_op, _copy_op,
_rename_op, _query_network, _find_children, _cook_op.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPOperators(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _create_op ---

    def test_create_op_valid(self):
        result = self.claudius._create_op(
            parent_path=self.sandbox.path, op_type='baseCOMP', name='test_comp')
        self.assertDictHasKey(result, 'path')
        self.assertIn('test_comp', result['path'])

    def test_create_op_without_name(self):
        result = self.claudius._create_op(
            parent_path=self.sandbox.path, op_type='textDAT')
        self.assertDictHasKey(result, 'path')

    def test_create_op_invalid_parent(self):
        result = self.claudius._create_op(
            parent_path='/nonexistent/path', op_type='baseCOMP')
        self.assertDictHasKey(result, 'error')

    def test_create_op_in_non_comp(self):
        dat = self.sandbox.create(textDAT, 'not_a_comp')
        result = self.claudius._create_op(
            parent_path=dat.path, op_type='baseCOMP')
        self.assertDictHasKey(result, 'error')

    # --- _delete_op ---

    def test_delete_op_existing(self):
        comp = self.sandbox.create(baseCOMP, 'to_delete')
        result = self.claudius._delete_op(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_delete_op_nonexistent(self):
        result = self.claudius._delete_op(op_path='/nonexistent/op')
        self.assertDictHasKey(result, 'error')

    # --- _get_op ---

    def test_get_op_returns_info(self):
        comp = self.sandbox.create(baseCOMP, 'info_target')
        result = self.claudius._get_op(op_path=comp.path)
        self.assertEqual(result['name'], 'info_target')
        self.assertDictHasKey(result, 'type')
        self.assertDictHasKey(result, 'family')

    def test_get_op_has_parameters(self):
        comp = self.sandbox.create(baseCOMP, 'par_target')
        result = self.claudius._get_op(op_path=comp.path)
        self.assertDictHasKey(result, 'parameters')

    def test_get_op_nonexistent(self):
        result = self.claudius._get_op(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _copy_op ---

    def test_copy_op_basic(self):
        source = self.sandbox.create(baseCOMP, 'source')
        result = self.claudius._copy_op(
            source_path=source.path, dest_parent=self.sandbox.path, new_name='copy')
        self.assertDictHasKey(result, 'new_path')
        self.assertIn('copy', result['new_path'])

    def test_copy_op_without_name(self):
        source = self.sandbox.create(baseCOMP, 'source2')
        result = self.claudius._copy_op(
            source_path=source.path, dest_parent=self.sandbox.path)
        self.assertDictHasKey(result, 'new_path')

    # --- _rename_op ---

    def test_rename_op(self):
        comp = self.sandbox.create(baseCOMP, 'old_name')
        result = self.claudius._rename_op(
            op_path=comp.path, new_name='new_name')
        self.assertTrue(result.get('success'))

    def test_rename_op_nonexistent(self):
        result = self.claudius._rename_op(
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

    # --- _cook_op ---

    def test_cook_op(self):
        comp = self.sandbox.create(baseCOMP, 'cookable')
        result = self.claudius._cook_op(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_cook_op_nonexistent(self):
        result = self.claudius._cook_op(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')
