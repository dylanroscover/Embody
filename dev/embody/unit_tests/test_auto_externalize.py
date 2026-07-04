"""
Test suite: Auto-externalization of Envoy-created ops.

Covers _autoExternalizeTagFor -- the pure decision behind AutoExternalizeNewOp,
which create_op calls after every successful create. Tests the preference family
gating (Neither / DATs / COMPs / both), tag selection (COMP -> tdn, DAT -> source
tag), and the boundary skips (non-externalizable family, already-externalized
ancestor, idempotence). The side-effecting apply + settle-debounced flush are
verified live end-to-end; here we exercise the decision matrix without file I/O.

NOTE: the whole test harness lives under /embody, which is itself a TDN-
externalized COMP -- so the framework's self.sandbox is ALWAYS inside an
externalized ancestor and every decision there correctly returns None. To test
the "returns a tag at a real boundary" cases, these tests build ops in a
throwaway root-level container (a sibling of /embody, at a genuine boundary).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestAutoExternalize(EmbodyTestCase):

    def setUp(self):
        # Preserve the live preference so tests never leave auto-externalize on.
        self._prior_mode = self.embody.par.Autoexternalize.eval()
        # Throwaway root-level container = a real externalization boundary
        # (outside /embody, not inside any externalized ancestor). Created raw
        # (not via create_op) and untagged, so it is not itself externalized.
        existing = op('/ae_test_container')
        if existing:
            existing.destroy()
        self._box = op('/').create('baseCOMP', 'ae_test_container')
        self._box.nodeX, self._box.nodeY = -2000, -2000

    def tearDown(self):
        self.embody.par.Autoexternalize = self._prior_mode
        try:
            if self._box:
                self._box.destroy()
        except Exception:
            pass
        super().tearDown()

    def _decide(self, oper):
        """The pure decision -- no side effects, no files written."""
        return self.embody_ext._autoExternalizeTagFor(oper)

    # --- preference off ---

    def test_neither_skips_comp(self):
        self.embody.par.Autoexternalize = 'neither'
        c = self._box.create('baseCOMP', 'ae_neither_comp')
        self.assertIsNone(self._decide(c))

    def test_neither_skips_dat(self):
        self.embody.par.Autoexternalize = 'neither'
        d = self._box.create('textDAT', 'ae_neither_dat')
        self.assertIsNone(self._decide(d))

    # --- family gating ---

    def test_comps_only_tags_comp_not_dat(self):
        self.embody.par.Autoexternalize = 'comps'
        c = self._box.create('baseCOMP', 'ae_co_comp')
        d = self._box.create('textDAT', 'ae_co_dat')
        self.assertEqual(self._decide(c), self.embody.par.Tdntag.val)
        self.assertIsNone(self._decide(d))

    def test_dats_only_tags_dat_not_comp(self):
        self.embody.par.Autoexternalize = 'dats'
        c = self._box.create('baseCOMP', 'ae_do_comp')
        d = self._box.create('textDAT', 'ae_do_dat')
        self.assertIsNone(self._decide(c))
        self.assertEqual(self._decide(d), self.embody_ext._inferDATTagValue(d))

    def test_both_tags_comp_and_dat(self):
        self.embody.par.Autoexternalize = 'both'
        c = self._box.create('baseCOMP', 'ae_both_comp')
        d = self._box.create('textDAT', 'ae_both_dat')
        self.assertEqual(self._decide(c), self.embody.par.Tdntag.val)
        self.assertIsNotNone(self._decide(d))

    # --- non-externalizable family ---

    def test_top_never_externalized(self):
        self.embody.par.Autoexternalize = 'both'
        t = self._box.create('nullTOP', 'ae_null_top')
        self.assertIsNone(self._decide(t))

    # --- boundary: op already captured by an externalized ancestor ---

    def test_skips_op_inside_tdn_ancestor(self):
        self.embody.par.Autoexternalize = 'both'
        parent = self._box.create('baseCOMP', 'ae_tdn_parent')
        # Raw tag add (no export) to simulate an already-externalized ancestor.
        parent.tags.add(self.embody.par.Tdntag.val)
        child = parent.create('baseCOMP', 'ae_child')
        child_dat = parent.create('textDAT', 'ae_child_dat')
        self.assertIsNone(self._decide(child))
        self.assertIsNone(self._decide(child_dat))

    def test_skips_op_inside_tox_ancestor(self):
        self.embody.par.Autoexternalize = 'both'
        parent = self._box.create('baseCOMP', 'ae_tox_parent')
        parent.tags.add(self.embody.par.Toxtag.val)
        child = parent.create('textDAT', 'ae_tox_child')
        self.assertIsNone(self._decide(child))

    # --- idempotence ---

    def test_already_tagged_comp_returns_none(self):
        self.embody.par.Autoexternalize = 'both'
        c = self._box.create('baseCOMP', 'ae_already')
        c.tags.add(self.embody.par.Tdntag.val)
        self.assertIsNone(self._decide(c))

    # --- copy path: inherited-state reset (the copy footgun) ---
    # A copy inherits the source's externalization tags + a DAT's file par.
    # _resetInheritedExternalization must clear that so the copy externalizes
    # fresh at its own path and never shares the source's files. (The full
    # externalize-a-copy behavior is verified live end-to-end.)

    def test_reset_clears_inherited_comp_tag(self):
        c = self._box.create('baseCOMP', 'ae_reset_comp')
        c.tags.add(self.embody.par.Tdntag.val)
        if hasattr(c.par, 'externaltox'):
            c.par.externaltox = 'ae_probe/stale.tox'
        self.embody_ext._resetInheritedExternalization(c)
        self.assertNotIn(self.embody.par.Tdntag.val, c.tags)
        if hasattr(c.par, 'externaltox'):
            self.assertEqual(c.par.externaltox.eval(), '')

    def test_reset_clears_inherited_dat_tag_and_file(self):
        d = self._box.create('textDAT', 'ae_reset_dat')
        d.tags.add(self.embody.par.Pytag.val)
        d.par.file = 'ae_probe/stale.py'
        self.embody_ext._resetInheritedExternalization(d)
        self.assertNotIn(self.embody.par.Pytag.val, d.tags)
        self.assertEqual(d.par.file.eval(), '')

    def test_reset_recurses_into_comp_descendants(self):
        parent = self._box.create('baseCOMP', 'ae_reset_parent')
        child = parent.create('textDAT', 'ae_reset_child')
        child.tags.add(self.embody.par.Pytag.val)
        child.par.file = 'ae_probe/stale_child.py'
        self.embody_ext._resetInheritedExternalization(parent)
        self.assertNotIn(self.embody.par.Pytag.val, child.tags)
        self.assertEqual(child.par.file.eval(), '')

    def test_copied_op_pref_off_does_not_strip(self):
        # With the pref off, a copy keeps its inherited tags untouched.
        self.embody.par.Autoexternalize = 'neither'
        c = self._box.create('baseCOMP', 'ae_copy_off')
        c.tags.add(self.embody.par.Tdntag.val)
        r = self.embody_ext.AutoExternalizeCopiedOp(c)
        self.assertIsNone(r)
        self.assertIn(self.embody.par.Tdntag.val, c.tags)

    def test_copied_op_gates_on_family(self):
        # pref = comps -> a copied DAT is left alone (not this family).
        self.embody.par.Autoexternalize = 'comps'
        d = self._box.create('textDAT', 'ae_copy_fam')
        d.tags.add(self.embody.par.Pytag.val)
        r = self.embody_ext.AutoExternalizeCopiedOp(d)
        self.assertIsNone(r)
        self.assertIn(self.embody.par.Pytag.val, d.tags)

    # --- method surface + param contract ---

    def test_methods_exist(self):
        self.assertTrue(hasattr(self.embody_ext, 'AutoExternalizeNewOp'))
        self.assertTrue(hasattr(self.embody_ext, '_autoExternalizeTagFor'))
        self.assertTrue(hasattr(self.embody_ext, '_scheduleAutoExternalizeFlush'))

    def test_param_menu_contract(self):
        p = self.embody.par.Autoexternalize
        self.assertEqual(list(p.menuNames), ['neither', 'dats', 'comps', 'both'])
        self.assertEqual(p.default, 'neither')
