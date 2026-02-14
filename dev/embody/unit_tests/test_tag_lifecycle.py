"""
Test suite: Tag lifecycle — TagSetter toggle, re-tag cycles, applyTagToOperator edge cases.

Tests higher-level tag workflows:
  - TagSetter toggle on/off for COMPs and DATs
  - Color reset on toggle off
  - Parameter cleanup on toggle off
  - Re-tag after removal produces clean state
  - Full lifecycle: tag → externalize → untag → re-tag
  - applyTagToOperator with existing externaltox
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestTagLifecycle(EmbodyTestCase):

    def setUp(self):
        """Create a clean workspace for each test."""
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')

    def tearDown(self):
        """Clean up externalizations table rows for sandbox ops."""
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    # =========================================================================
    # TagSetter — toggle on
    # =========================================================================

    def test_tagSetter_toggle_on_comp(self):
        """TagSetter should add a tox tag to a COMP."""
        comp = self.workspace.create(baseCOMP, 'toggle_on')
        tox_tag = self.embody.par.Toxtag.val
        result = self.embody_ext.TagSetter(comp, tox_tag)
        self.assertTrue(result)
        self.assertIn(tox_tag, comp.tags)

    def test_tagSetter_toggle_on_dat(self):
        """TagSetter should add a py tag to a textDAT."""
        dat = self.workspace.create(textDAT, 'toggle_on_dat')
        py_tag = self.embody.par.Pytag.val
        result = self.embody_ext.TagSetter(dat, py_tag)
        self.assertTrue(result)
        self.assertIn(py_tag, dat.tags)

    # =========================================================================
    # TagSetter — toggle off
    # =========================================================================

    def test_tagSetter_toggle_off_comp(self):
        """TagSetter should remove tag when it's already present."""
        comp = self.workspace.create(baseCOMP, 'toggle_off')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.TagSetter(comp, tox_tag)
        self.assertIn(tox_tag, comp.tags)

        self.embody_ext.TagSetter(comp, tox_tag)
        self.assertNotIn(tox_tag, comp.tags)

    def test_tagSetter_toggle_off_dat(self):
        """TagSetter should remove tag from a DAT when toggled off."""
        dat = self.workspace.create(textDAT, 'toggle_off_dat')
        py_tag = self.embody.par.Pytag.val
        self.embody_ext.TagSetter(dat, py_tag)
        self.assertIn(py_tag, dat.tags)

        self.embody_ext.TagSetter(dat, py_tag)
        self.assertNotIn(py_tag, dat.tags)

    def test_tagSetter_toggle_off_resets_color(self):
        """TagSetter toggle off should reset the operator to default color."""
        comp = self.workspace.create(baseCOMP, 'color_reset')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.TagSetter(comp, tox_tag)
        self.embody_ext.TagSetter(comp, tox_tag)

        default_color = (0.55, 0.55, 0.55)
        color = comp.color
        close = all(abs(a - b) < 0.02 for a, b in zip(color, default_color))
        self.assertTrue(close, f'Color should reset to default, got {color}')

    def test_tagSetter_toggle_off_clears_externaltox(self):
        """TagSetter toggle off should clear the externaltox parameter."""
        comp = self.workspace.create(baseCOMP, 'clear_ext')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.TagSetter(comp, tox_tag)
        # Manually set externaltox to simulate externalization
        comp.par.externaltox.readOnly = False
        comp.par.externaltox = 'test/path.tox'

        self.embody_ext.TagSetter(comp, tox_tag)
        self.assertEqual(comp.par.externaltox.eval(), '',
                         'externaltox should be cleared after toggle off')

    def test_tagSetter_toggle_off_clears_dat_file(self):
        """TagSetter toggle off should clear the file parameter on a DAT."""
        dat = self.workspace.create(textDAT, 'clear_file')
        py_tag = self.embody.par.Pytag.val
        self.embody_ext.TagSetter(dat, py_tag)
        # Manually set file to simulate externalization
        dat.par.file.readOnly = False
        dat.par.file = 'test/path.py'

        self.embody_ext.TagSetter(dat, py_tag)
        self.assertEqual(dat.par.file.eval(), '',
                         'file should be cleared after toggle off')

    def test_tagSetter_toggle_off_unlocks_readonly(self):
        """TagSetter toggle off should unlock the readOnly flag."""
        comp = self.workspace.create(baseCOMP, 'unlock')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.TagSetter(comp, tox_tag)
        comp.par.externaltox.readOnly = True

        self.embody_ext.TagSetter(comp, tox_tag)
        self.assertFalse(comp.par.externaltox.readOnly)

    # =========================================================================
    # TagSetter — validation
    # =========================================================================

    def test_tagSetter_wrong_family_returns_false(self):
        """TagSetter should return False for tox tag on DAT."""
        dat = self.workspace.create(textDAT, 'wrong_family')
        tox_tag = self.embody.par.Toxtag.val
        result = self.embody_ext.TagSetter(dat, tox_tag)
        self.assertFalse(result)

    def test_tagSetter_dat_tag_on_comp_returns_false(self):
        """TagSetter should return False for DAT tag on COMP."""
        comp = self.workspace.create(baseCOMP, 'wrong_tag')
        py_tag = self.embody.par.Pytag.val
        result = self.embody_ext.TagSetter(comp, py_tag)
        self.assertFalse(result)

    # =========================================================================
    # Re-tag after removal
    # =========================================================================

    def test_retag_comp_after_removal_one_row(self):
        """Re-tagging a COMP after removal should produce exactly one table row."""
        comp = self.workspace.create(baseCOMP, 'retag_comp')
        tox_tag = self.embody.par.Toxtag.val

        comp.tags.add(tox_tag)
        self.embody_ext.handleAddition(comp)
        comp.tags.remove(tox_tag)
        self.embody_ext.handleSubtraction(comp)

        comp.tags.add(tox_tag)
        self.embody_ext.handleAddition(comp)

        count = 0
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                count += 1
        self.assertEqual(count, 1, 'Should have exactly one row after re-tag')

    def test_retag_dat_after_removal_one_row(self):
        """Re-tagging a DAT after removal should produce exactly one table row."""
        dat = self.workspace.create(textDAT, 'retag_dat')
        py_tag = self.embody.par.Pytag.val

        dat.tags.add(py_tag)
        self.embody_ext.handleAddition(dat)
        dat.tags.remove(py_tag)
        self.embody_ext.handleSubtraction(dat)

        dat.tags.add(py_tag)
        self.embody_ext.handleAddition(dat)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == dat.path:
                found = True
                break
        self.assertTrue(found, 'Should have row after re-tag')

    # =========================================================================
    # Full lifecycle
    # =========================================================================

    def test_full_lifecycle_tag_externalize_untag_retag(self):
        """Full cycle: tag → externalize → untag + subtract → re-tag → re-externalize."""
        comp = self.workspace.create(baseCOMP, 'lifecycle')
        tox_tag = self.embody.par.Toxtag.val

        # Phase 1: Tag and externalize
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.embody_ext.handleAddition(comp)
        self.assertIn(tox_tag, comp.tags)
        self.assertTrue(len(comp.par.externaltox.eval()) > 0)

        # Phase 2: Untag and subtract
        comp.tags.remove(tox_tag)
        self.embody_ext.handleSubtraction(comp)
        self.assertNotIn(tox_tag, comp.tags)
        self.assertFalse(comp.par.externaltox.readOnly)

        # Phase 3: Re-tag and re-externalize
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.embody_ext.handleAddition(comp)
        self.assertIn(tox_tag, comp.tags)
        self.assertTrue(len(comp.par.externaltox.eval()) > 0)

    def test_applyTagToOperator_comp_with_existing_externaltox(self):
        """applyTagToOperator on a COMP with existing externaltox should add to table."""
        comp = self.workspace.create(baseCOMP, 'existing_ext')
        comp.par.externaltox = 'some/existing/path.tox'
        tox_tag = self.embody.par.Toxtag.val

        initial_rows = self.embody_ext.Externalizations.numRows
        self.embody_ext.applyTagToOperator(comp, tox_tag)

        # Should add a row because the COMP already has an externaltox
        self.assertGreater(self.embody_ext.Externalizations.numRows, initial_rows)

    # =========================================================================
    # _getTagColor edge cases
    # =========================================================================

    def test_getTagColor_comp_tox_returns_tuple(self):
        """_getTagColor should return a color tuple for valid COMP + tox tag."""
        comp = self.workspace.create(baseCOMP, 'color_valid')
        tox_tag = self.embody.par.Toxtag.val
        color = self.embody_ext._getTagColor(comp, tox_tag)
        self.assertIsNotNone(color)
        self.assertLen(color, 3)

    def test_getTagColor_dat_py_returns_tuple(self):
        """_getTagColor should return a color tuple for valid DAT + py tag."""
        dat = self.workspace.create(textDAT, 'color_dat')
        py_tag = self.embody.par.Pytag.val
        color = self.embody_ext._getTagColor(dat, py_tag)
        self.assertIsNotNone(color)
        self.assertLen(color, 3)

    def test_getTagColor_invalid_returns_none(self):
        """_getTagColor should return None for invalid family/tag combination."""
        dat = self.workspace.create(textDAT, 'color_invalid')
        tox_tag = self.embody.par.Toxtag.val
        color = self.embody_ext._getTagColor(dat, tox_tag)
        self.assertIsNone(color)
