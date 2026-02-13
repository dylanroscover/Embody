"""
Test suite: TDN export/import round-trip.

Tests ExportNetwork, ImportNetwork, max_depth, DAT content,
clear_first, format validation, and round-trip fidelity.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNExportImport(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN

    # --- ExportNetwork ---

    def test_export_returns_success(self):
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        self.assertTrue(result.get('success'))

    def test_export_returns_tdn_dict(self):
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        self.assertDictHasKey(result, 'tdn')
        self.assertIsInstance(result['tdn'], dict)

    def test_export_tdn_has_format_fields(self):
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        tdn = result['tdn']
        self.assertDictHasKey(tdn, 'format')
        self.assertEqual(tdn['format'], 'tdn')
        self.assertDictHasKey(tdn, 'version')
        self.assertDictHasKey(tdn, 'root')

    def test_export_includes_children(self):
        self.sandbox.create(baseCOMP, 'child_a')
        self.sandbox.create(textDAT, 'child_b')
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        tdn = result['tdn']
        names = [o['name'] for o in tdn['operators']]
        self.assertIn('child_a', names)
        self.assertIn('child_b', names)

    def test_export_nonexistent_path(self):
        result = self.tdn.ExportNetwork(root_path='/nonexistent_tdn_test')
        self.assertDictHasKey(result, 'error')

    def test_export_non_comp(self):
        dat = self.sandbox.create(textDAT, 'not_comp')
        result = self.tdn.ExportNetwork(root_path=dat.path)
        # Non-COMPs may succeed with empty operators or return error
        if 'error' not in result:
            self.assertTrue(result.get('success'))

    def test_export_empty_comp(self):
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(len(result['tdn']['operators']), 0)

    def test_export_max_depth_zero(self):
        parent = self.sandbox.create(baseCOMP, 'depth_parent')
        parent.create(baseCOMP, 'depth_child')
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, max_depth=0)
        tdn = result['tdn']
        # Depth 0 should export direct children but not recurse
        names = [o['name'] for o in tdn['operators']]
        self.assertIn('depth_parent', names)

    def test_export_dat_content_included(self):
        dat = self.sandbox.create(textDAT, 'content_dat')
        dat.text = 'hello world'
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        tdn = result['tdn']
        dat_entry = None
        for o in tdn['operators']:
            if o['name'] == 'content_dat':
                dat_entry = o
                break
        self.assertIsNotNone(dat_entry)
        self.assertDictHasKey(dat_entry, 'dat_content')

    def test_export_dat_content_excluded(self):
        dat = self.sandbox.create(textDAT, 'nocontent_dat')
        dat.text = 'secret'
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=False)
        tdn = result['tdn']
        dat_entry = None
        for o in tdn['operators']:
            if o['name'] == 'nocontent_dat':
                dat_entry = o
                break
        self.assertIsNotNone(dat_entry)
        # Should not have dat_content key
        self.assertNotIn('dat_content', dat_entry)

    # --- ImportNetwork ---

    def test_import_basic(self):
        # Export a sandbox with children, import into a fresh COMP
        self.sandbox.create(baseCOMP, 'imp_child')
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'import_target')
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=export['tdn'])
        self.assertTrue(result.get('success'))
        self.assertGreaterEqual(result['created_count'], 1)

    def test_import_creates_operators(self):
        self.sandbox.create(baseCOMP, 'src_comp')
        self.sandbox.create(textDAT, 'src_dat')
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'imp_target2')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        child_names = [c.name for c in target.children]
        self.assertIn('src_comp', child_names)
        self.assertIn('src_dat', child_names)

    def test_import_clear_first(self):
        target = self.sandbox.create(baseCOMP, 'clear_target')
        target.create(baseCOMP, 'existing_child')
        tdn = {'operators': []}
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=tdn, clear_first=True)
        self.assertTrue(result.get('success'))
        self.assertEqual(len(target.children), 0)

    def test_import_nonexistent_target(self):
        result = self.tdn.ImportNetwork(
            target_path='/nonexistent_imp', tdn={'operators': []})
        self.assertDictHasKey(result, 'error')

    def test_import_invalid_tdn(self):
        target = self.sandbox.create(baseCOMP, 'invalid_target')
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn='not a dict')
        self.assertDictHasKey(result, 'error')

    def test_import_operators_array_directly(self):
        self.sandbox.create(baseCOMP, 'arr_child')
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'arr_target')
        # Pass just the operators list instead of full tdn dict
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=export['tdn']['operators'])
        self.assertTrue(result.get('success'))

    # --- Round-trip fidelity ---

    def test_roundtrip_preserves_operator_names(self):
        self.sandbox.create(baseCOMP, 'rt_alpha')
        self.sandbox.create(textDAT, 'rt_beta')
        self.sandbox.create(noiseTOP, 'rt_gamma')
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'roundtrip_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        child_names = sorted([c.name for c in target.children])
        self.assertIn('rt_alpha', child_names)
        self.assertIn('rt_beta', child_names)
        self.assertIn('rt_gamma', child_names)

    def test_roundtrip_preserves_dat_content(self):
        dat = self.sandbox.create(textDAT, 'rt_text')
        dat.text = 'round trip content'
        export = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        target = self.sandbox.create(baseCOMP, 'rt_content_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        imported_dat = target.op('rt_text')
        self.assertIsNotNone(imported_dat)
        self.assertEqual(imported_dat.text, 'round trip content')

    def test_roundtrip_preserves_nested_structure(self):
        parent = self.sandbox.create(baseCOMP, 'rt_parent')
        parent.create(baseCOMP, 'rt_nested_child')
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'rt_nested_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        imported_parent = target.op('rt_parent')
        self.assertIsNotNone(imported_parent)
        nested = imported_parent.op('rt_nested_child')
        self.assertIsNotNone(nested)
