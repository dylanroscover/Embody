"""
Test suite: MCP flags and position handlers in EnvoyExt.

Tests _get_op_flags, _set_op_flags, _get_op_position,
_set_op_position, _layout_children, _get_network_layout.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPFlagsPosition(EmbodyTestCase):

    def setUp(self):
        super().setUp()

    # --- _get_op_flags ---

    def test_get_op_flags(self):
        comp = self.sandbox.create(baseCOMP, 'flags_target')
        result = self.embody.ext.Envoy._get_op_flags(op_path=comp.path)
        self.assertDictHasKey(result, 'bypass')
        self.assertDictHasKey(result, 'lock')
        self.assertDictHasKey(result, 'display')

    def test_get_op_flags_nonexistent(self):
        result = self.embody.ext.Envoy._get_op_flags(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _set_op_flags ---

    def test_set_op_flags_bypass(self):
        comp = self.sandbox.create(baseCOMP, 'bypass_test')
        result = self.embody.ext.Envoy._set_op_flags(
            op_path=comp.path, bypass=True)
        self.assertNotIn('error', result)
        self.assertDictHasKey(result, 'bypass')

    def test_set_op_flags_multiple(self):
        dat = self.sandbox.create(textDAT, 'multi_flags')
        result = self.embody.ext.Envoy._set_op_flags(
            op_path=dat.path, bypass=True, lock=True)
        self.assertNotIn('error', result)
        self.assertDictHasKey(result, 'bypass')

    def test_set_op_flags_nonexistent(self):
        result = self.embody.ext.Envoy._set_op_flags(
            op_path='/nonexistent', bypass=True)
        self.assertDictHasKey(result, 'error')

    # --- _get_op_position ---

    def test_get_op_position(self):
        comp = self.sandbox.create(baseCOMP, 'pos_target')
        result = self.embody.ext.Envoy._get_op_position(op_path=comp.path)
        self.assertDictHasKey(result, 'nodeX')
        self.assertDictHasKey(result, 'nodeY')

    def test_get_op_position_has_color(self):
        comp = self.sandbox.create(baseCOMP, 'color_target')
        result = self.embody.ext.Envoy._get_op_position(op_path=comp.path)
        self.assertDictHasKey(result, 'color')

    # --- _set_op_position ---

    def test_set_op_position_xy(self):
        comp = self.sandbox.create(baseCOMP, 'move_target')
        result = self.embody.ext.Envoy._set_op_position(
            op_path=comp.path, x=100, y=200)
        self.assertNotIn('error', result)
        self.assertDictHasKey(result, 'nodeX')

    def test_set_op_position_color(self):
        comp = self.sandbox.create(baseCOMP, 'color_set')
        result = self.embody.ext.Envoy._set_op_position(
            op_path=comp.path, color=[1.0, 0.0, 0.0])
        self.assertNotIn('error', result)
        self.assertDictHasKey(result, 'color')

    def test_set_op_position_comment(self):
        comp = self.sandbox.create(baseCOMP, 'comment_set')
        result = self.embody.ext.Envoy._set_op_position(
            op_path=comp.path, comment='Test comment')
        self.assertNotIn('error', result)
        self.assertDictHasKey(result, 'comment')

    # --- _layout_children ---

    def test_layout_children(self):
        parent = self.sandbox.create(baseCOMP, 'layout_parent')
        parent.create(baseCOMP, 'child1')
        parent.create(baseCOMP, 'child2')
        result = self.embody.ext.Envoy._layout_children(op_path=parent.path)
        self.assertTrue(result.get('success'))

    def test_layout_children_nonexistent(self):
        result = self.embody.ext.Envoy._layout_children(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _get_network_layout ---

    def test_get_network_layout_lists_children(self):
        parent = self.sandbox.create(baseCOMP, 'layout_query')
        parent.create(baseCOMP, 'child_a')
        parent.create(baseCOMP, 'child_b')
        result = self.embody.ext.Envoy._get_network_layout(comp_path=parent.path)
        self.assertDictHasKey(result, 'operators')
        self.assertEqual(result['count'], 2)
        names = {o['name'] for o in result['operators']}
        self.assertEqual(names, {'child_a', 'child_b'})

    def test_get_network_layout_entry_has_position_fields(self):
        parent = self.sandbox.create(baseCOMP, 'layout_fields')
        parent.create(baseCOMP, 'only_child')
        result = self.embody.ext.Envoy._get_network_layout(comp_path=parent.path)
        entry = result['operators'][0]
        for key in ('nodeX', 'nodeY', 'nodeWidth', 'nodeHeight'):
            self.assertDictHasKey(entry, key)
        self.assertDictHasKey(result, 'bounding_box')

    def test_get_network_layout_annotations_toggle(self):
        parent = self.sandbox.create(baseCOMP, 'layout_anno')
        parent.create(baseCOMP, 'a_child')
        with_anno = self.embody.ext.Envoy._get_network_layout(
            comp_path=parent.path, include_annotations=True)
        self.assertDictHasKey(with_anno, 'annotations')
        without = self.embody.ext.Envoy._get_network_layout(
            comp_path=parent.path, include_annotations=False)
        self.assertNotIn('annotations', without)

    def test_get_network_layout_nonexistent(self):
        result = self.embody.ext.Envoy._get_network_layout(comp_path='/nonexistent')
        self.assertDictHasKey(result, 'error')
