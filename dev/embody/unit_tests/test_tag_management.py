"""
Test suite: Tag management methods in EmbodyExt.

Tests getTags, applyTagToOperator, isOpEligibleToBeExternalized,
isOpProcessable, isInsideClone, isClone, isReplicant.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTagManagement(EmbodyTestCase):

    # --- getTags ---

    def test_getTags_returns_list(self):
        result = self.embody_ext.getTags()
        self.assertIsInstance(result, list)

    def test_getTags_has_tox(self):
        tags = self.embody_ext.getTags()
        tox_tag = self.embody.par.Toxtag.val
        self.assertIn(tox_tag, tags)

    def test_getTags_filter_tox(self):
        tags = self.embody_ext.getTags('tox')
        tox_tag = self.embody.par.Toxtag.val
        self.assertLen(tags, 1)
        self.assertEqual(tags[0], tox_tag)

    def test_getTags_filter_dat(self):
        tags = self.embody_ext.getTags('DAT')
        tox_tag = self.embody.par.Toxtag.val
        self.assertNotIn(tox_tag, tags)
        self.assertGreater(len(tags), 0)

    # --- applyTagToOperator ---

    def test_applyTagToOperator_adds_tag(self):
        comp = self.sandbox.create(baseCOMP, 'tag_test')
        tox_tag = self.embody.par.Toxtag.val
        result = self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.assertTrue(result)
        self.assertIn(tox_tag, comp.tags)

    def test_applyTagToOperator_sets_color(self):
        comp = self.sandbox.create(baseCOMP, 'color_test')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        # Color should have changed from default
        default_color = (0.545, 0.545, 0.545)
        color = comp.color
        differs = any(abs(a - b) > 0.01 for a, b in zip(color, default_color))
        self.assertTrue(differs, 'Color should change after tagging')

    def test_applyTagToOperator_duplicate_tag_no_error(self):
        comp = self.sandbox.create(baseCOMP, 'dup_tag')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        # Apply same tag again - should not error
        result = self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.assertTrue(result)

    # --- isOpEligibleToBeExternalized ---

    def test_isOpEligible_comp_is_eligible(self):
        comp = self.sandbox.create(baseCOMP, 'eligible_comp')
        self.assertTrue(self.embody_ext.isOpEligibleToBeExternalized(comp))

    def test_isOpEligible_text_dat_without_tag_not_eligible(self):
        dat = self.sandbox.create(textDAT, 'no_tag_dat')
        self.assertFalse(self.embody_ext.isOpEligibleToBeExternalized(dat))

    # --- isOpProcessable ---

    def test_isOpProcessable_normal_comp(self):
        comp = self.sandbox.create(baseCOMP, 'normal')
        self.assertTrue(self.embody_ext.isOpProcessable(comp))

    def test_isOpProcessable_normal_dat(self):
        dat = self.sandbox.create(textDAT, 'normal_dat')
        self.assertTrue(self.embody_ext.isOpProcessable(dat))

    # --- isClone ---

    def test_isClone_regular_comp_not_clone(self):
        comp = self.sandbox.create(baseCOMP, 'not_clone')
        self.assertFalse(self.embody_ext.isClone(comp))

    def test_isClone_dat_not_clone(self):
        dat = self.sandbox.create(textDAT, 'not_clone_dat')
        self.assertFalse(self.embody_ext.isClone(dat))

    # --- isInsideClone ---

    def test_isInsideClone_regular_op_false(self):
        comp = self.sandbox.create(baseCOMP, 'outer')
        inner = comp.create(textDAT, 'inner')
        self.assertFalse(self.embody_ext.isInsideClone(inner))

    def test_isInsideClone_handles_missing_clone_par(self):
        """isInsideClone should not raise on ops without par.clone."""
        dat = self.sandbox.create(textDAT, 'plain_dat')
        # DATs have no par.clone -- should return False, not raise
        self.assertFalse(self.embody_ext.isInsideClone(dat))

    def test_isInsideClone_dat_inside_clone_comp_true(self):
        """DAT inside an active clone COMP should return True."""
        master = self.sandbox.create(baseCOMP, 'clone_master')
        clone = self.sandbox.create(baseCOMP, 'clone_instance')
        clone.par.clone = master
        clone.par.enablecloning = True
        inner_dat = clone.create(textDAT, 'ext_dat')
        self.assertTrue(self.embody_ext.isInsideClone(inner_dat))

    def test_isInsideClone_dat_inside_master_comp_false(self):
        """DAT inside the master COMP should return False."""
        master = self.sandbox.create(baseCOMP, 'the_master')
        clone = self.sandbox.create(baseCOMP, 'the_clone')
        clone.par.clone = master
        clone.par.enablecloning = True
        master_dat = master.create(textDAT, 'ext_dat')
        self.assertFalse(self.embody_ext.isInsideClone(master_dat))

    def test_isInsideClone_clone_comp_itself_true(self):
        """A clone COMP itself should return True (preserves call-site behavior)."""
        master = self.sandbox.create(baseCOMP, 'comp_master')
        clone = self.sandbox.create(baseCOMP, 'comp_clone')
        clone.par.clone = master
        clone.par.enablecloning = True
        self.assertTrue(self.embody_ext.isInsideClone(clone))

    def test_isInsideClone_disabled_cloning_false(self):
        """COMP with clone set but enablecloning=False should return False."""
        master = self.sandbox.create(baseCOMP, 'dis_master')
        clone = self.sandbox.create(baseCOMP, 'dis_clone')
        clone.par.clone = master
        clone.par.enablecloning = False
        inner_dat = clone.create(textDAT, 'dis_dat')
        self.assertFalse(self.embody_ext.isInsideClone(inner_dat))

    def test_isClone_actual_clone_returns_true(self):
        """isClone returns True for a COMP that is an active clone."""
        master = self.sandbox.create(baseCOMP, 'ic_master')
        clone = self.sandbox.create(baseCOMP, 'ic_clone')
        clone.par.clone = master
        clone.par.enablecloning = True
        self.assertTrue(self.embody_ext.isClone(clone))

    def test_isClone_master_returns_false(self):
        """isClone returns False for the master COMP."""
        master = self.sandbox.create(baseCOMP, 'ic_master2')
        clone = self.sandbox.create(baseCOMP, 'ic_clone2')
        clone.par.clone = master
        clone.par.enablecloning = True
        self.assertFalse(self.embody_ext.isClone(master))

    def test_isClone_self_reference_is_master(self):
        """A COMP with par.clone self-referencing is a master, not a clone.

        Reproduces the reusable-UI-component pattern where par.clone
        evaluates to self via an expression like iop.Components.op('MyComp').
        Uses expression mode to avoid TD's clone-sync recursion on direct
        self-assignment with cloning enabled.
        """
        master = self.sandbox.create(baseCOMP, 'self_ref_master')
        master.par.clone.expr = f"op('{master.path}')"
        master.par.enablecloning = True
        self.assertFalse(self.embody_ext.isClone(master),
            'Self-referencing COMP should NOT be identified as clone')

    def test_isInsideClone_self_reference_master_false(self):
        """DAT inside a self-referencing master should return False."""
        master = self.sandbox.create(baseCOMP, 'sr_host')
        master.par.clone.expr = f"op('{master.path}')"
        master.par.enablecloning = True
        inner_dat = master.create(textDAT, 'ext_dat')
        self.assertFalse(self.embody_ext.isInsideClone(inner_dat),
            'DAT inside self-referencing master should NOT be marked inside-clone')

    def test_isInsideClone_self_reference_comp_itself_false(self):
        """A self-referencing master COMP itself should return False."""
        master = self.sandbox.create(baseCOMP, 'sr_comp')
        master.par.clone.expr = f"op('{master.path}')"
        master.par.enablecloning = True
        self.assertFalse(self.embody_ext.isInsideClone(master),
            'Self-referencing master should NOT be marked inside-clone')

    # --- isReplicant ---

    def test_isReplicant_regular_op_false(self):
        comp = self.sandbox.create(baseCOMP, 'no_replicator')
        self.assertFalse(self.embody_ext.isReplicant(comp))

    # --- isOpEligibleToBeExternalized edge cases (BUG 2 regression) ---

    def test_isOpEligible_tagged_dat_is_eligible(self):
        """A DAT with a valid tag should be eligible (regression for BUG 2 fix)."""
        dat = self.sandbox.create(textDAT, 'tagged_dat')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)
        self.assertTrue(self.embody_ext.isOpEligibleToBeExternalized(dat))

    def test_isOpEligible_tagged_dat_with_file_set(self):
        """A tagged DAT with file already set should still be eligible."""
        dat = self.sandbox.create(textDAT, 'file_dat')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)
        dat.par.file = 'some/path.py'
        self.assertTrue(self.embody_ext.isOpEligibleToBeExternalized(dat))

    def test_isOpEligible_comp_with_existing_externaltox(self):
        """A COMP with existing externaltox should still be eligible."""
        comp = self.sandbox.create(baseCOMP, 'ext_comp')
        comp.par.externaltox = 'existing/path.tox'
        self.assertTrue(self.embody_ext.isOpEligibleToBeExternalized(comp))

    def test_isOpEligible_untagged_dat_not_eligible(self):
        """A DAT without any Embody tag should not be eligible."""
        dat = self.sandbox.create(textDAT, 'untagged')
        self.assertFalse(self.embody_ext.isOpEligibleToBeExternalized(dat))
