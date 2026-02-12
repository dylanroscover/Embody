"""
Test suite: Tag management methods in EmbodyExt.

Tests getTags, applyTagToOperator, isOpEligibleToBeExternalized,
isOpProcessable, isInsideClone, isClone, isReplicant.
"""

runner_mod = op('TestRunner').module
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
        # Apply same tag again — should not error
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

    # --- isReplicant ---

    def test_isReplicant_regular_op_false(self):
        comp = self.sandbox.create(baseCOMP, 'no_replicator')
        self.assertFalse(self.embody_ext.isReplicant(comp))
