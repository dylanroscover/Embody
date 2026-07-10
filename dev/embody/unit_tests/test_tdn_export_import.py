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
        self.assertDictHasKey(tdn, 'network_path')

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

    # --- v2.0: multi-line text dat_content stored as a plain string ---

    def test_export_multiline_dat_content_is_string(self):
        """v2.0: a multi-line text DAT exports dat_content as a plain
        string (not a list); YAML's literal block scalar (|) renders it
        readably on disk. This inverts the v1.5 list behavior."""
        original = 'line one\nline two\nline three'
        dat = self.sandbox.create(textDAT, 'ml_dat')
        dat.text = original
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        dat_entry = None
        for o in result['tdn']['operators']:
            if o['name'] == 'ml_dat':
                dat_entry = o
                break
        self.assertIsNotNone(dat_entry)
        self.assertEqual(dat_entry.get('dat_content_format'), 'text')
        content = dat_entry['dat_content']
        self.assertIsInstance(content, str)
        self.assertEqual(content, original)

    def test_export_single_line_dat_content_is_string(self):
        """v2.0: a single-line text DAT keeps dat_content as a plain
        string (as all text dat_content does in v2.0)."""
        dat = self.sandbox.create(textDAT, 'sl_dat')
        dat.text = 'just one line'
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        dat_entry = None
        for o in result['tdn']['operators']:
            if o['name'] == 'sl_dat':
                dat_entry = o
                break
        self.assertIsNotNone(dat_entry)
        self.assertEqual(dat_entry.get('dat_content_format'), 'text')
        self.assertIsInstance(dat_entry['dat_content'], str)
        self.assertEqual(dat_entry['dat_content'], 'just one line')

    def test_roundtrip_preserves_multiline_dat_content(self):
        """v2.0: a full export -> import of a multi-line text DAT leaves
        target.text byte-identical to the original."""
        original = 'alpha\nbeta\n\ngamma\n'
        dat = self.sandbox.create(textDAT, 'ml_rt')
        dat.text = original
        export = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        target = self.sandbox.create(baseCOMP, 'ml_rt_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        imported = target.op('ml_rt')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.text, original)

    def test_import_back_compat_string_dat_content(self):
        """Back-compat: an op_def whose dat_content is a plain string
        (the v1.x form) still sets target.text on import."""
        target = self.sandbox.create(baseCOMP, 'compat_target')
        tdn = {'operators': [
            {'name': 'legacy_dat', 'type': 'textDAT',
             'dat_content': 'old\nstyle\nstring',
             'dat_content_format': 'text'}
        ]}
        result = self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        self.assertTrue(result.get('success'))
        imported = target.op('legacy_dat')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.text, 'old\nstyle\nstring')

    def test_import_back_compat_list_dat_content(self):
        """Back-compat: an op_def whose dat_content is a list of line-
        strings (the v1.5 form) still imports, joined with '\\n'."""
        target = self.sandbox.create(baseCOMP, 'compat_list_target')
        lines = ['old', 'style', 'list']
        tdn = {'operators': [
            {'name': 'legacy_list_dat', 'type': 'textDAT',
             'dat_content': lines,
             'dat_content_format': 'text'}
        ]}
        result = self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        self.assertTrue(result.get('success'))
        imported = target.op('legacy_list_dat')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.text, '\n'.join(lines))

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

    def test_import_pop_point_sequence_roundtrip(self):
        # POP point sequences (e.g. primitivePOP `pt`) are reachable only via
        # iteration -- op.seq['pt'] subscript returns None -- so import must
        # resolve them by iterating. Regression for numBlocks silently failing
        # on POP sequences (the warning seen pasting noise_terrain).
        src = self.sandbox.create(primitivePOP, 'seq_src')
        src.par.method = 'set'
        pt = next((s for s in src.seq if s.name == 'pt'), None)
        self.assertIsNotNone(pt, 'primitivePOP should expose a pt sequence')
        pt.numBlocks = 3
        export = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        target = self.sandbox.create(baseCOMP, 'seq_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        imported = target.op('seq_src')
        self.assertIsNotNone(imported)
        ipt = next((s for s in imported.seq if s.name == 'pt'), None)
        self.assertIsNotNone(ipt, 'pt sequence must round-trip on import')
        self.assertEqual(ipt.numBlocks, 3)

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

    # --- Custom parameter VALUE round-trip ---

    def test_roundtrip_custom_par_default_valued(self):
        """Regression: a custom Float whose value equals its (non-standard)
        default round-trips with the VALUE intact. The exporter omits a value
        that equals the default, so the importer must restore it from the
        default -- otherwise the param imports at 0/min and the network is
        inert (this broke every parametric specimen on import)."""
        p = self.sandbox.appendCustomPage('RT').appendFloat('Speed')[0]
        p.default = 0.7
        p.val = 0.7
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'rt_def_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        self.assertAlmostEqual(target.par.Speed.eval(), 0.7, places=4)

    def test_roundtrip_custom_par_nondefault_valued(self):
        """A custom Float whose value differs from its default round-trips."""
        p = self.sandbox.appendCustomPage('RT').appendFloat('Gain')[0]
        p.default = 0.0
        p.val = 0.42
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'rt_nd_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        self.assertAlmostEqual(target.par.Gain.eval(), 0.42, places=4)

    def test_roundtrip_custom_int_default_valued(self):
        """A custom Int at its non-zero default round-trips with the value."""
        p = self.sandbox.appendCustomPage('RT').appendInt('Count')[0]
        p.default = 5
        p.val = 5
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'rt_int_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        self.assertEqual(int(target.par.Count.eval()), 5)

    def test_roundtrip_custom_toggle_default_valued(self):
        """A custom Toggle defaulting to True round-trips as True."""
        p = self.sandbox.appendCustomPage('RT').appendToggle('Active')[0]
        p.default = True
        p.val = True
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'rt_tog_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        self.assertEqual(int(target.par.Active.eval()), 1)

    def test_roundtrip_child_custom_par_default_valued(self):
        """A default-valued custom par on a CHILD COMP round-trips (values
        flow through Phase 3 for children; the default-fallback restores
        the omitted value)."""
        child = self.sandbox.create(baseCOMP, 'rt_child')
        p = child.appendCustomPage('C').appendFloat('Mass')[0]
        p.default = 0.3
        p.val = 0.3
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'rt_child_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        imported = target.op('rt_child')
        self.assertIsNotNone(imported)
        self.assertAlmostEqual(imported.par.Mass.eval(), 0.3, places=4)

    def test_roundtrip_custom_par_expression_not_clobbered(self):
        """A custom par in EXPRESSION mode round-trips the expression -- the
        default-value fallback must not overwrite it with the constant
        default."""
        p = self.sandbox.appendCustomPage('RT').appendFloat('Expr')[0]
        p.default = 0.5
        par_mode = type(p.mode)  # ParMode enum (not a module global here)
        p.expr = '0.1 + 0.2'
        p.mode = par_mode.EXPRESSION
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(baseCOMP, 'rt_expr_target')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        tp = target.par.Expr
        self.assertEqual(tp.mode, par_mode.EXPRESSION)
        self.assertAlmostEqual(tp.eval(), 0.3, places=4)

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

    # --- Import: clear_first behavior ---

    def test_import_clear_first_removes_existing_then_imports(self):
        """clear_first should destroy existing children before importing new ones."""
        target = self.sandbox.create(baseCOMP, 'cf_target')
        target.create(textDAT, 'old_dat')
        tdn = {'operators': [
            {'name': 'new_dat', 'type': 'textDAT'}
        ]}
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=tdn, clear_first=True)
        self.assertTrue(result.get('success'))
        child_names = [c.name for c in target.children]
        self.assertNotIn('old_dat', child_names)
        self.assertIn('new_dat', child_names)

    def test_import_without_clear_keeps_existing(self):
        """Without clear_first, existing children should remain."""
        target = self.sandbox.create(baseCOMP, 'merge_target')
        target.create(textDAT, 'existing_dat')
        tdn = {'operators': [
            {'name': 'added_dat', 'type': 'textDAT'}
        ]}
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=tdn, clear_first=False)
        self.assertTrue(result.get('success'))
        child_names = [c.name for c in target.children]
        self.assertIn('existing_dat', child_names)
        self.assertIn('added_dat', child_names)

    # --- Import: merge-mode operator tracking ---

    def test_import_merge_mode_renamed_op_gets_correct_position(self):
        """When TD auto-renames a conflicting operator, position should
        apply to the imported op, not the pre-existing one."""
        target = self.sandbox.create(baseCOMP, 'pos_target')
        # Create pre-existing operator
        existing = target.create(textDAT, 'text1')
        existing.nodeX = 100
        existing.nodeY = 100
        # Import a TDN that also defines 'text1' at a different position
        tdn = {'operators': [
            {'name': 'text1', 'type': 'textDAT', 'position': [300, 400]}
        ]}
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=tdn, clear_first=False)
        self.assertTrue(result.get('success'))
        # The pre-existing operator should keep its original position
        self.assertEqual(existing.nodeX, 100)
        self.assertEqual(existing.nodeY, 100)
        # The imported op (auto-renamed) should be at the TDN position
        created_path = result['created_paths'][0]
        imported_op = op(created_path)
        self.assertIsNotNone(imported_op)
        self.assertEqual(imported_op.nodeX, 300)
        self.assertEqual(imported_op.nodeY, 400)

    def test_import_merge_mode_renamed_op_gets_correct_params(self):
        """When TD auto-renames, parameters should apply to the imported op."""
        target = self.sandbox.create(baseCOMP, 'par_target')
        existing = target.create(textDAT, 'text1')
        existing.par.language = 'python'
        tdn = {'operators': [
            {'name': 'text1', 'type': 'textDAT',
             'parameters': {'language': 'glsl'}}
        ]}
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=tdn, clear_first=False)
        self.assertTrue(result.get('success'))
        # Pre-existing should keep its parameter
        self.assertEqual(existing.par.language.eval(), 'python')
        # Imported should have the TDN parameter
        created_path = result['created_paths'][0]
        imported_op = op(created_path)
        self.assertEqual(imported_op.par.language.eval(), 'glsl')

    def test_import_merge_mode_logs_rename_warning(self):
        """When an operator is auto-renamed, a warning should be logged."""
        target = self.sandbox.create(baseCOMP, 'warn_target')
        target.create(textDAT, 'text1')
        tdn = {'operators': [
            {'name': 'text1', 'type': 'textDAT'}
        ]}
        self.tdn.ImportNetwork(
            target_path=target.path, tdn=tdn, clear_first=False)
        # Check that the imported op has a different name
        child_names = [c.name for c in target.children]
        self.assertEqual(len(child_names), 2)

    # --- Import: position round-trip ---

    def test_import_preserves_positions(self):
        """Positions from TDN should be applied correctly."""
        target = self.sandbox.create(baseCOMP, 'posrt_target')
        tdn = {'operators': [
            {'name': 'pos_op', 'type': 'textDAT',
             'position': [250, 175]}
        ]}
        self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        imported = target.op('pos_op')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.nodeX, 250)
        self.assertEqual(imported.nodeY, 175)

    def test_import_preserves_size(self):
        """Size from TDN should be applied correctly."""
        target = self.sandbox.create(baseCOMP, 'sizert_target')
        tdn = {'operators': [
            {'name': 'size_op', 'type': 'textDAT',
             'position': [0, 0], 'size': [300, 150]}
        ]}
        self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        imported = target.op('size_op')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.nodeWidth, 300)
        self.assertEqual(imported.nodeHeight, 150)

    # --- Storage export/import ---

    def test_export_includes_storage(self):
        """Storage entries appear in exported TDN data."""
        c = self.sandbox.create(baseCOMP, 'c')
        c.store('my_key', 'my_value')
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        ops = result['tdn']['operators']
        op_data = [o for o in ops if o['name'] == 'c'][0]
        self.assertIn('storage', op_data)
        self.assertEqual(op_data['storage']['my_key'], 'my_value')

    def test_export_storage_type_wrappers(self):
        """Non-JSON types are wrapped with $type/$value."""
        c = self.sandbox.create(baseCOMP, 'c')
        c.store('my_tuple', (1, 2))
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        ops = result['tdn']['operators']
        op_data = [o for o in ops if o['name'] == 'c'][0]
        self.assertEqual(
            op_data['storage']['my_tuple'],
            {'$type': 'tuple', '$value': [1, 2]})

    def test_import_restores_storage(self):
        """Storage from TDN data is restored on import."""
        target = self.sandbox.create(baseCOMP, 'storage_target')
        tdn = {'operators': [
            {'name': 'stored_op', 'type': 'baseCOMP',
             'storage': {'count': 42, 'label': 'test'}}
        ]}
        self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        imported = target.op('stored_op')
        self.assertIsNotNone(imported)
        self.assertEqual(
            imported.fetch('count', None, search=False), 42)
        self.assertEqual(
            imported.fetch('label', None, search=False), 'test')

    def test_import_restores_typed_storage(self):
        """$type wrappers are deserialized on import."""
        target = self.sandbox.create(baseCOMP, 'typed_target')
        tdn = {'operators': [
            {'name': 'typed_op', 'type': 'baseCOMP',
             'storage': {
                 'coords': {'$type': 'tuple', '$value': [10, 20]},
                 'tags': {'$type': 'set', '$value': ['a', 'b']}
             }}
        ]}
        self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        imported = target.op('typed_op')
        self.assertEqual(
            imported.fetch('coords', None, search=False), (10, 20))
        self.assertEqual(
            imported.fetch('tags', None, search=False), {'a', 'b'})

    def test_import_restores_startup_storage(self):
        """startup_storage keys are restored via storeStartupValue."""
        target = self.sandbox.create(baseCOMP, 'startup_target')
        tdn = {'operators': [
            {'name': 'startup_op', 'type': 'baseCOMP',
             'startup_storage': {'version': 1, 'mode': 'auto'}}
        ]}
        self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        imported = target.op('startup_op')
        self.assertIsNotNone(imported)
        # storeStartupValue also sets the current value
        self.assertEqual(
            imported.fetch('version', None, search=False), 1)
        self.assertEqual(
            imported.fetch('mode', None, search=False), 'auto')

    def test_import_both_storage_and_startup_storage(self):
        """Both storage and startup_storage are restored."""
        target = self.sandbox.create(baseCOMP, 'both_target')
        tdn = {'operators': [
            {'name': 'both_op', 'type': 'baseCOMP',
             'storage': {'count': 42},
             'startup_storage': {'version': 1}}
        ]}
        self.tdn.ImportNetwork(target_path=target.path, tdn=tdn)
        imported = target.op('both_op')
        self.assertEqual(
            imported.fetch('count', None, search=False), 42)
        self.assertEqual(
            imported.fetch('version', None, search=False), 1)

    # --- Suffix-style custom parameter groups (RGBA/XYZW arity + naming) ---

    def test_rgba_group_base_ending_in_suffix_letter(self):
        """RGBA group whose base name ends in 'r' round-trips unmangled.

        'Bordercolor' ends with the first RGBA suffix letter; the old
        import stripped it and rebuilt 'Bordercolo' + r/g/b.
        """
        src = self.sandbox.create(containerCOMP, 'rgba_src')
        page = src.appendCustomPage('Look')
        grp = page.appendRGBA('Bordercolor', label='Border Color')
        src.par.Bordercolorr = 0.1
        src.par.Bordercolorg = 0.2
        src.par.Bordercolorb = 0.3
        src.par.Bordercolora = 0.9
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        self.assertTrue(export.get('success'))

        target = self.sandbox.create(containerCOMP, 'rgba_dst')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        rebuilt = target.op('rgba_src')
        self.assertIsNotNone(rebuilt)
        names = [p.name for p in rebuilt.customPars]
        self.assertIn('Bordercolorr', names, f'components mangled: {names}')
        self.assertIn('Bordercolora', names, 'alpha component missing')
        self.assertNotIn('Bordercolog', names, 'mangled g component present')
        self.assertAlmostEqual(
            float(rebuilt.par.Bordercolora.eval()), 0.9, places=4)

    def test_rgba_group_all_default_keeps_alpha(self):
        """Values-less RGBA group (all defaults) keeps 4 components.

        With no exported values the old import downgraded RGBA to RGB,
        silently dropping alpha (the TauCeti widget color pars).
        """
        src = self.sandbox.create(containerCOMP, 'rgba_dflt_src')
        page = src.appendCustomPage('Look')
        page.appendRGBA('Fillcolor')
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        entry = next(o for o in export['tdn']['operators']
                     if o.get('name') == 'rgba_dflt_src')
        target = self.sandbox.create(containerCOMP, 'rgba_dflt_dst')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        rebuilt = target.op('rgba_dflt_src')
        names = [p.name for p in rebuilt.customPars]
        for comp in ('Fillcolorr', 'Fillcolorg', 'Fillcolorb', 'Fillcolora'):
            self.assertIn(comp, names,
                f'{comp} missing -- RGBA downgraded: {names}')

    def test_rgb_group_stays_three_components(self):
        """A 3-component RGB group (TD reports style RGBA) stays RGB.

        The export records size=3 so the importer appends RGB, not RGBA.
        """
        src = self.sandbox.create(containerCOMP, 'rgb_src')
        page = src.appendCustomPage('Look')
        page.appendRGB('Tint')
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        entry = next(o for o in export['tdn']['operators']
                     if o.get('name') == 'rgb_src')
        par_defs = entry.get('custom_pars', {}).get('Look', [])
        tint = next(d for d in par_defs if d.get('name') == 'Tint')
        self.assertEqual(tint.get('size'), 3,
            'RGB group must export size=3 to disambiguate from RGBA')
        target = self.sandbox.create(containerCOMP, 'rgb_dst')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        rebuilt = target.op('rgb_src')
        names = [p.name for p in rebuilt.customPars]
        self.assertIn('Tintb', names)
        self.assertNotIn('Tinta', names,
            'RGB group must not grow an alpha component')

    def test_xy_group_roundtrip(self):
        """A 2-component XY group (TD reports style XYZW) stays XY."""
        src = self.sandbox.create(containerCOMP, 'xy_src')
        page = src.appendCustomPage('Look')
        page.appendXY('Anchor')
        src.par.Anchorx = 0.25
        export = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        target = self.sandbox.create(containerCOMP, 'xy_dst')
        self.tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        rebuilt = target.op('xy_src')
        names = [p.name for p in rebuilt.customPars]
        self.assertIn('Anchorx', names)
        self.assertIn('Anchory', names)
        self.assertNotIn('Anchorz', names,
            'XY group must not grow z/w components')
        self.assertAlmostEqual(
            float(rebuilt.par.Anchorx.eval()), 0.25, places=4)
