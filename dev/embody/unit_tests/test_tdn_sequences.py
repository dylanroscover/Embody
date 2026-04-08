"""
Test suite: TDN built-in parameter sequence round-trip (v1.3).

Tests export, import, and round-trip fidelity for operators with
built-in parameter sequences (constantCHOP const blocks, etc.).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# ParMode is a TD builtin global but not on the td module.
# Obtain the enum type from an existing parameter's mode.
ParMode = type(op('/').par.clone.mode)


class TestTDNSequences(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN

    # --- Helper ---

    def _export(self, **kwargs):
        defaults = {'root_path': self.sandbox.path}
        defaults.update(kwargs)
        return self.tdn.ExportNetwork(**defaults)

    def _roundtrip(self, **export_kwargs):
        """Export sandbox, create target, import with clear, return target."""
        export = self._export(**export_kwargs)
        target = self.sandbox.create(baseCOMP, 'rt_target')
        self.tdn.ImportNetwork(
            target_path=target.path, tdn=export['tdn'])
        return target

    # --- Export tests ---

    def test_sequence_export_includes_sequences_key(self):
        chop = self.sandbox.create(constantCHOP, 'seq_chop')
        seq = chop.seq.const
        seq.numBlocks = 2
        seq[0].par.name = 'chan_a'
        seq[0].par.value = 42.0
        seq[1].par.name = 'chan_b'
        seq[1].par.value = 99.0
        result = self._export()
        tdn = result['tdn']
        chop_def = None
        for o in tdn['operators']:
            if o['name'] == 'seq_chop':
                chop_def = o
                break
        self.assertIsNotNone(chop_def)
        self.assertDictHasKey(chop_def, 'sequences')
        self.assertIn('const', chop_def['sequences'])

    def test_sequence_default_blocks_omitted(self):
        # A fresh constantCHOP with 1 default block should have no sequences key
        self.sandbox.create(constantCHOP, 'default_chop')
        result = self._export()
        tdn = result['tdn']
        chop_def = None
        for o in tdn['operators']:
            if o['name'] == 'default_chop':
                chop_def = o
                break
        self.assertIsNotNone(chop_def)
        self.assertNotIn('sequences', chop_def)

    def test_sequence_base_names_no_prefix(self):
        chop = self.sandbox.create(constantCHOP, 'basenames_chop')
        seq = chop.seq.const
        seq.numBlocks = 2
        seq[0].par.name = 'test_chan'
        seq[0].par.value = 1.0
        result = self._export()
        tdn = result['tdn']
        chop_def = [o for o in tdn['operators']
                    if o['name'] == 'basenames_chop'][0]
        blocks = chop_def['sequences']['const']
        # Keys should be base names like 'name' and 'value',
        # NOT prefixed names like 'const0name' or 'const0value'
        for block in blocks:
            for key in block:
                self.assertFalse(
                    key.startswith('const'),
                    f'Expected base name, got prefixed: {key}')

    def test_sequence_nondefault_value_in_first_block_exported(self):
        chop = self.sandbox.create(constantCHOP, 'val_chop')
        seq = chop.seq.const
        seq[0].par.name = 'my_chan'
        seq[0].par.value = 7.5
        result = self._export()
        tdn = result['tdn']
        chop_def = [o for o in tdn['operators']
                    if o['name'] == 'val_chop'][0]
        self.assertDictHasKey(chop_def, 'sequences')
        blocks = chop_def['sequences']['const']
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].get('name'), 'my_chan')
        self.assertEqual(blocks[0].get('value'), 7.5)

    # --- Round-trip tests ---

    def test_sequence_roundtrip_constantchop(self):
        chop = self.sandbox.create(constantCHOP, 'rt_chop')
        seq = chop.seq.const
        seq.numBlocks = 3
        seq[0].par.name = 'alpha'
        seq[0].par.value = 1.0
        seq[1].par.name = 'beta'
        seq[1].par.value = 2.5
        seq[2].par.name = 'gamma'
        seq[2].par.value = -0.5

        target = self._roundtrip()
        imp = target.op('rt_chop')
        self.assertIsNotNone(imp)
        imp_seq = imp.seq.const
        self.assertEqual(imp_seq.numBlocks, 3)
        self.assertEqual(imp_seq[0].par.name.eval(), 'alpha')
        self.assertEqual(imp_seq[0].par.value.eval(), 1.0)
        self.assertEqual(imp_seq[1].par.name.eval(), 'beta')
        self.assertEqual(imp_seq[1].par.value.eval(), 2.5)
        self.assertEqual(imp_seq[2].par.name.eval(), 'gamma')
        self.assertEqual(imp_seq[2].par.value.eval(), -0.5)

    def test_sequence_roundtrip_multiple_blocks(self):
        chop = self.sandbox.create(constantCHOP, 'multi_chop')
        seq = chop.seq.const
        seq.numBlocks = 5
        for i in range(5):
            seq[i].par.name = f'ch{i}'
            seq[i].par.value = float(i * 10)

        target = self._roundtrip()
        imp = target.op('multi_chop')
        imp_seq = imp.seq.const
        self.assertEqual(imp_seq.numBlocks, 5)
        for i in range(5):
            self.assertEqual(imp_seq[i].par.name.eval(), f'ch{i}')
            self.assertEqual(imp_seq[i].par.value.eval(), float(i * 10))

    def test_sequence_roundtrip_empty_blocks(self):
        chop = self.sandbox.create(constantCHOP, 'empty_chop')
        seq = chop.seq.const
        # Some operators may not support 0 blocks; test with default values
        seq.numBlocks = 2
        # Leave all values at defaults
        target = self._roundtrip()
        imp = target.op('empty_chop')
        imp_seq = imp.seq.const
        self.assertEqual(imp_seq.numBlocks, 2)

    def test_sequence_roundtrip_expression_values(self):
        chop = self.sandbox.create(constantCHOP, 'expr_chop')
        seq = chop.seq.const
        seq[0].par.name = 'expr_chan'
        chop.par.const0value.expr = 'absTime.seconds'
        chop.par.const0value.mode = ParMode.EXPRESSION

        result = self._export()
        tdn = result['tdn']
        chop_def = [o for o in tdn['operators']
                    if o['name'] == 'expr_chop'][0]
        blocks = chop_def['sequences']['const']
        # Expression should be stored with '=' prefix
        self.assertTrue(
            str(blocks[0].get('value', '')).startswith('='),
            f'Expected expression prefix, got: {blocks[0].get("value")}')

        target = self._roundtrip()
        imp = target.op('expr_chop')
        imp_par = imp.par.const0value
        self.assertEqual(imp_par.mode, ParMode.EXPRESSION)
        self.assertEqual(imp_par.expr, 'absTime.seconds')

    def test_sequence_type_defaults_exclude_sequences(self):
        # Create two constantCHOPs with different sequence data
        c1 = self.sandbox.create(constantCHOP, 'td_chop1')
        c1.seq.const[0].par.name = 'shared'
        c1.seq.const[0].par.value = 1.0
        c2 = self.sandbox.create(constantCHOP, 'td_chop2')
        c2.seq.const[0].par.name = 'shared'
        c2.seq.const[0].par.value = 1.0

        result = self._export()
        tdn = result['tdn']
        # type_defaults should NOT contain 'sequences'
        td = tdn.get('type_defaults', {}).get('constantCHOP', {})
        self.assertNotIn('sequences', td)

    def test_sequence_absent_key_preserves_defaults(self):
        # Import a TDN without sequences key — existing blocks unaltered
        chop = self.sandbox.create(constantCHOP, 'nokey_chop')
        chop.seq.const[0].par.name = 'keep_me'
        chop.seq.const[0].par.value = 42.0

        op_def = {
            'operators': [{
                'name': 'nokey_chop',
                'type': 'constantCHOP',
                'parameters': {},
            }]
        }
        target = self.sandbox.create(baseCOMP, 'nokey_target')
        target.create(constantCHOP, 'nokey_chop')
        target.op('nokey_chop').seq.const[0].par.name = 'preset'
        target.op('nokey_chop').seq.const[0].par.value = 99.0

        self.tdn.ImportNetwork(target_path=target.path, tdn=op_def)
        imp = target.op('nokey_chop')
        # Without sequences key, existing blocks should not be altered
        self.assertEqual(imp.seq.const[0].par.name.eval(), 'preset')

    def test_sequence_nested_comp_sequences(self):
        parent = self.sandbox.create(baseCOMP, 'nest_parent')
        chop = parent.create(constantCHOP, 'nested_chop')
        chop.seq.const.numBlocks = 2
        chop.seq.const[0].par.name = 'inner_a'
        chop.seq.const[0].par.value = 10.0
        chop.seq.const[1].par.name = 'inner_b'
        chop.seq.const[1].par.value = 20.0

        target = self._roundtrip()
        imp_parent = target.op('nest_parent')
        self.assertIsNotNone(imp_parent)
        imp_chop = imp_parent.op('nested_chop')
        self.assertIsNotNone(imp_chop)
        imp_seq = imp_chop.seq.const
        self.assertEqual(imp_seq.numBlocks, 2)
        self.assertEqual(imp_seq[0].par.name.eval(), 'inner_a')
        self.assertEqual(imp_seq[0].par.value.eval(), 10.0)
        self.assertEqual(imp_seq[1].par.name.eval(), 'inner_b')
        self.assertEqual(imp_seq[1].par.value.eval(), 20.0)

    # --- POP family: mathmixPOP (the original bug report) ---

    def test_sequence_roundtrip_mathmixpop_combine(self):
        pop = self.sandbox.create(mathmixPOP, 'rt_mathmix')
        seq = pop.seq.comb
        seq.numBlocks = 3
        # Menu parameters use menu NAMES, not labels:
        # 'copya' = "A", 'add' = "A + B", 'normalize' = "normalize(A)"
        seq[0].par.oper = 'copya'
        seq[0].par.scopea = 'P'
        seq[0].par.result = 'startPos'
        seq[1].par.oper = 'add'
        seq[1].par.scopea = 'vel'
        seq[1].par.scopeb = 'direction'
        seq[1].par.result = 'vel'
        seq[2].par.oper = 'normalize'
        seq[2].par.scopea = 'vel'
        seq[2].par.result = 'direction'

        target = self._roundtrip()
        imp = target.op('rt_mathmix')
        self.assertIsNotNone(imp)
        imp_seq = imp.seq.comb
        self.assertEqual(imp_seq.numBlocks, 3)
        self.assertEqual(imp_seq[0].par.oper.eval(), 'copya')
        self.assertEqual(imp_seq[0].par.scopea.eval(), 'P')
        self.assertEqual(imp_seq[0].par.result.eval(), 'startPos')
        self.assertEqual(imp_seq[1].par.oper.eval(), 'add')
        self.assertEqual(imp_seq[1].par.scopea.eval(), 'vel')
        self.assertEqual(imp_seq[1].par.scopeb.eval(), 'direction')
        self.assertEqual(imp_seq[1].par.result.eval(), 'vel')
        self.assertEqual(imp_seq[2].par.oper.eval(), 'normalize')

    def test_sequence_roundtrip_mathmixpop_multi_sequences(self):
        pop = self.sandbox.create(mathmixPOP, 'rt_mathmix_multi')
        # Set comb sequence
        pop.seq.comb.numBlocks = 2
        pop.seq.comb[0].par.oper = 'A'
        pop.seq.comb[0].par.scopea = 'P'
        pop.seq.comb[0].par.result = 'out'
        pop.seq.comb[1].par.oper = 'A + B'
        pop.seq.comb[1].par.result = 'sum'
        # Set vec uniform sequence
        pop.seq.vec[0].par.name = 'uTest'

        target = self._roundtrip()
        imp = target.op('rt_mathmix_multi')
        self.assertEqual(imp.seq.comb.numBlocks, 2)
        self.assertEqual(imp.seq.comb[0].par.result.eval(), 'out')
        self.assertEqual(imp.seq.comb[1].par.result.eval(), 'sum')
        self.assertEqual(imp.seq.vec[0].par.name.eval(), 'uTest')

    # --- POP family: attributePOP ---

    def test_sequence_roundtrip_attributepop(self):
        pop = self.sandbox.create(attributePOP, 'rt_attrpop')
        seq = pop.seq.attr
        seq.numBlocks = 2
        seq[0].par.name = 'P'
        seq[0].par.customname = 'startPos'
        seq[1].par.name = 'custom'
        seq[1].par.customname = 'myAttr'

        target = self._roundtrip()
        imp = target.op('rt_attrpop')
        imp_seq = imp.seq.attr
        self.assertEqual(imp_seq.numBlocks, 2)
        self.assertEqual(imp_seq[0].par.customname.eval(), 'startPos')
        self.assertEqual(imp_seq[1].par.customname.eval(), 'myAttr')

    # --- TOP family: glslTOP (many sequences) ---

    def test_sequence_roundtrip_glsltop_vec(self):
        top = self.sandbox.create(glslTOP, 'rt_glsl')
        seq = top.seq.vec
        seq.numBlocks = 3
        seq[0].par.name = 'uColor'
        seq[0].par.valuex = 1.0
        seq[0].par.valuey = 0.5
        seq[0].par.valuez = 0.0
        seq[1].par.name = 'uScale'
        seq[1].par.valuex = 2.0
        seq[2].par.name = 'uOffset'
        seq[2].par.valuex = -1.0
        seq[2].par.valuey = 3.0

        target = self._roundtrip()
        imp = target.op('rt_glsl')
        imp_seq = imp.seq.vec
        self.assertEqual(imp_seq.numBlocks, 3)
        self.assertEqual(imp_seq[0].par.name.eval(), 'uColor')
        self.assertEqual(imp_seq[0].par.valuex.eval(), 1.0)
        self.assertEqual(imp_seq[0].par.valuey.eval(), 0.5)
        self.assertEqual(imp_seq[1].par.name.eval(), 'uScale')
        self.assertEqual(imp_seq[1].par.valuex.eval(), 2.0)
        self.assertEqual(imp_seq[2].par.name.eval(), 'uOffset')
        self.assertEqual(imp_seq[2].par.valuey.eval(), 3.0)

    # --- SOP family: addSOP ---

    def test_sequence_roundtrip_addsop_points(self):
        sop = self.sandbox.create(addSOP, 'rt_add')
        seq = sop.seq.point
        seq.numBlocks = 3
        seq[0].par.posx = 1.0
        seq[0].par.posy = 2.0
        seq[0].par.posz = 3.0
        seq[1].par.posx = 4.0
        seq[1].par.posy = 5.0
        seq[2].par.posx = 7.0

        target = self._roundtrip()
        imp = target.op('rt_add')
        imp_seq = imp.seq.point
        self.assertEqual(imp_seq.numBlocks, 3)
        self.assertEqual(imp_seq[0].par.posx.eval(), 1.0)
        self.assertEqual(imp_seq[0].par.posy.eval(), 2.0)
        self.assertEqual(imp_seq[0].par.posz.eval(), 3.0)
        self.assertEqual(imp_seq[1].par.posx.eval(), 4.0)
        self.assertEqual(imp_seq[2].par.posx.eval(), 7.0)

    # --- MAT family: glslMAT ---

    def test_sequence_roundtrip_glslmat_vec(self):
        mat = self.sandbox.create(glslMAT, 'rt_glslmat')
        seq = mat.seq.vec
        seq.numBlocks = 2
        seq[0].par.name = 'uLightDir'
        seq[0].par.valuex = 0.0
        seq[0].par.valuey = 1.0
        seq[0].par.valuez = 0.0
        seq[1].par.name = 'uAmbient'
        seq[1].par.valuex = 0.2

        target = self._roundtrip()
        imp = target.op('rt_glslmat')
        imp_seq = imp.seq.vec
        self.assertEqual(imp_seq.numBlocks, 2)
        self.assertEqual(imp_seq[0].par.name.eval(), 'uLightDir')
        self.assertEqual(imp_seq[0].par.valuey.eval(), 1.0)
        self.assertEqual(imp_seq[1].par.name.eval(), 'uAmbient')
        self.assertEqual(imp_seq[1].par.valuex.eval(), 0.2)

    # --- COMP family: extension sequences ---

    def test_sequence_roundtrip_comp_ext_sequence(self):
        # All COMPs have ext and iop sequences for extensions/shortcuts.
        # Test that a COMP with 2 extension slots round-trips correctly.
        comp = self.sandbox.create(baseCOMP, 'rt_ext_comp')
        # ext sequence default is 1 block. The ext sequence pars are
        # object, name, promote — these are already handled by the
        # existing parameter export for the first block. Verify that
        # adding a second ext slot (numBlocks=2) round-trips.
        comp.seq.ext.numBlocks = 2

        target = self._roundtrip()
        imp = target.op('rt_ext_comp')
        self.assertIsNotNone(imp)
        self.assertEqual(imp.seq.ext.numBlocks, 2)

    # --- Custom parameter sequences ---

    def test_custom_sequence_export_skips_per_block_pars(self):
        # Verify that per-block instance pars (block index > 0) are NOT
        # exported as separate custom_par definitions.
        comp = self.sandbox.create(baseCOMP, 'cs_skip_test')
        page = comp.appendCustomPage('Items')
        page.appendSequence('Items', label='Items')
        page.appendStr('Itemlabel')
        page.appendFloat('Itemweight')
        comp.seq.Items.blockSize = 2
        comp.seq.Items.numBlocks = 3
        comp.par.Items0itemlabel = 'first'
        comp.par.Items1itemlabel = 'second'
        comp.par.Items2itemlabel = 'third'

        result = self._export()
        comp_def = [o for o in result['tdn']['operators']
                    if o['name'] == 'cs_skip_test'][0]
        custom_pars = comp_def.get('custom_pars', {}).get('Items', [])
        names = [p['name'] for p in custom_pars]
        # Should have header + 2 template pars only — no per-block instances
        self.assertIn('Items', names)
        self.assertEqual(len(custom_pars), 3)
        # No prefixed names should appear
        for name in names:
            self.assertFalse(
                name.startswith('Items0'),
                f'Per-block instance leaked: {name}')
            self.assertFalse(
                name.startswith('Items1'),
                f'Per-block instance leaked: {name}')

    def test_custom_sequence_template_pars_have_sequence_field(self):
        comp = self.sandbox.create(baseCOMP, 'cs_field_test')
        page = comp.appendCustomPage('Items')
        page.appendSequence('Items')
        page.appendStr('Itemlabel')
        page.appendFloat('Itemweight')
        comp.seq.Items.blockSize = 2
        comp.seq.Items.numBlocks = 1

        result = self._export()
        comp_def = [o for o in result['tdn']['operators']
                    if o['name'] == 'cs_field_test'][0]
        custom_pars = comp_def['custom_pars']['Items']
        # The two template pars (not the header) should have a 'sequence'
        # field marking them as belonging to the Items sequence
        non_header = [p for p in custom_pars if p.get('style') != 'Sequence']
        self.assertEqual(len(non_header), 2)
        for p in non_header:
            self.assertEqual(p.get('sequence'), 'Items')

    def test_custom_sequence_template_par_names_are_capitalized_base(self):
        comp = self.sandbox.create(baseCOMP, 'cs_name_test')
        page = comp.appendCustomPage('Items')
        page.appendSequence('Items')
        page.appendStr('Itemlabel')
        page.appendFloat('Itemweight')
        comp.seq.Items.blockSize = 2
        comp.seq.Items.numBlocks = 1

        result = self._export()
        comp_def = [o for o in result['tdn']['operators']
                    if o['name'] == 'cs_name_test'][0]
        custom_pars = comp_def['custom_pars']['Items']
        names = [p['name'] for p in custom_pars]
        # Template pars should have their base names capitalized
        self.assertIn('Itemlabel', names)
        self.assertIn('Itemweight', names)

    def test_custom_sequence_roundtrip_basic(self):
        comp = self.sandbox.create(baseCOMP, 'cs_rt_basic')
        page = comp.appendCustomPage('Items')
        page.appendSequence('Items', label='Items')
        page.appendStr('Itemlabel', label='Item Label')
        page.appendFloat('Itemweight', label='Item Weight')
        comp.seq.Items.blockSize = 2
        comp.seq.Items.numBlocks = 2
        comp.par.Items0itemlabel = 'first'
        comp.par.Items0itemweight = 1.5
        comp.par.Items1itemlabel = 'second'
        comp.par.Items1itemweight = 3.0

        target = self._roundtrip()
        imp = target.op('cs_rt_basic')
        self.assertIsNotNone(imp)
        seq = imp.seq.Items
        self.assertEqual(seq.numBlocks, 2)
        self.assertEqual(seq.blockSize, 2)
        self.assertEqual(imp.par.Items0itemlabel.eval(), 'first')
        self.assertEqual(imp.par.Items0itemweight.eval(), 1.5)
        self.assertEqual(imp.par.Items1itemlabel.eval(), 'second')
        self.assertEqual(imp.par.Items1itemweight.eval(), 3.0)

    def test_custom_sequence_roundtrip_three_blocks(self):
        comp = self.sandbox.create(baseCOMP, 'cs_rt_three')
        page = comp.appendCustomPage('Test')
        page.appendSequence('Items')
        page.appendStr('Itemlabel')
        page.appendFloat('Itemweight')
        comp.seq.Items.blockSize = 2
        comp.seq.Items.numBlocks = 3
        for i in range(3):
            setattr(comp.par, f'Items{i}itemlabel', f'item{i}')
            setattr(comp.par, f'Items{i}itemweight', float(i * 10))

        target = self._roundtrip()
        imp = target.op('cs_rt_three')
        self.assertEqual(imp.seq.Items.numBlocks, 3)
        for i in range(3):
            self.assertEqual(
                getattr(imp.par, f'Items{i}itemlabel').eval(), f'item{i}')
            self.assertEqual(
                getattr(imp.par, f'Items{i}itemweight').eval(), float(i * 10))

    def test_custom_sequence_roundtrip_label_preserved(self):
        comp = self.sandbox.create(baseCOMP, 'cs_rt_label')
        page = comp.appendCustomPage('Test')
        page.appendSequence('Items', label='My Items List')
        page.appendStr('Itemlabel', label='Item Display Label')
        comp.seq.Items.blockSize = 1
        comp.seq.Items.numBlocks = 1
        comp.par.Items0itemlabel = 'hello'

        target = self._roundtrip()
        imp = target.op('cs_rt_label')
        # Labels on template pars should be preserved
        self.assertEqual(imp.par.Items0itemlabel.label, 'Item Display Label')

    def test_custom_sequence_with_regular_pars_alongside(self):
        # A page with both regular custom pars AND a sequence
        comp = self.sandbox.create(baseCOMP, 'cs_mixed')
        page = comp.appendCustomPage('Mixed')
        page.appendFloat('Speed')[0].default = 1.0
        page.appendSequence('Items')
        page.appendStr('Itemlabel')
        comp.seq.Items.blockSize = 1
        comp.seq.Items.numBlocks = 2
        comp.par.Speed = 2.5
        comp.par.Items0itemlabel = 'a'
        comp.par.Items1itemlabel = 'b'

        target = self._roundtrip()
        imp = target.op('cs_mixed')
        self.assertEqual(imp.par.Speed.eval(), 2.5)
        self.assertEqual(imp.seq.Items.numBlocks, 2)
        self.assertEqual(imp.par.Items0itemlabel.eval(), 'a')
        self.assertEqual(imp.par.Items1itemlabel.eval(), 'b')

    def test_custom_sequence_default_blocksize_2(self):
        # Test blockSize > 1: 2 ParGroups per block (e.g. label + weight)
        comp = self.sandbox.create(baseCOMP, 'cs_bs2')
        page = comp.appendCustomPage('Test')
        page.appendSequence('Items')
        page.appendStr('Itemlabel')
        page.appendFloat('Itemweight')
        comp.seq.Items.blockSize = 2
        comp.seq.Items.numBlocks = 2

        # Verify the source has the right structure before testing
        self.assertEqual(comp.seq.Items.blockSize, 2)
        self.assertEqual(comp.seq.Items.numBlocks, 2)

        target = self._roundtrip()
        imp = target.op('cs_bs2')
        self.assertEqual(imp.seq.Items.blockSize, 2)
        self.assertEqual(imp.seq.Items.numBlocks, 2)

    # --- POP family: deletePOP (many sequences with complex params) ---

    def test_sequence_roundtrip_deletepop_attr(self):
        pop = self.sandbox.create(deletePOP, 'rt_delete')
        seq = pop.seq.attr
        seq.numBlocks = 2
        seq[0].par.inattr = 'state'
        seq[0].par.func = '>='
        seq[0].par.value = 1.0
        seq[1].par.inattr = 'age'
        seq[1].par.func = '>'
        seq[1].par.value = 100.0

        target = self._roundtrip()
        imp = target.op('rt_delete')
        imp_seq = imp.seq.attr
        self.assertEqual(imp_seq.numBlocks, 2)
        self.assertEqual(imp_seq[0].par.inattr.eval(), 'state')
        self.assertEqual(imp_seq[0].par.value.eval(), 1.0)
        self.assertEqual(imp_seq[1].par.inattr.eval(), 'age')
        self.assertEqual(imp_seq[1].par.value.eval(), 100.0)
