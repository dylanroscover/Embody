"""
Test suite: Strategy handlers — _removeExternalization, HandleStrategySwitch,
_dispatchTaggerButton, and manage-mode button dispatch logic.

Covers the manage-mode UI code paths that were missing test coverage:
  - _removeExternalization removes TOX/TDN without converting
  - HandleStrategySwitch converts TOX↔TDN
  - _dispatchTaggerButton routes by label text
  - Regression: Remove does NOT convert to the other strategy
"""

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestRemoveExternalization(EmbodyTestCase):

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')

    def tearDown(self):
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    # =========================================================================
    # _removeExternalization — TOX
    # =========================================================================

    def test_remove_tox_removes_tag(self):
        """_removeExternalization should remove the tox tag from a COMP."""
        comp = self.workspace.create(baseCOMP, 'rem_tox_tag')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.embody_ext.handleAddition(comp)

        self.embody_ext._removeExternalization(comp)
        self.assertNotIn(tox_tag, comp.tags)

    def test_remove_tox_clears_externaltox(self):
        """_removeExternalization should clear externaltox parameter."""
        comp = self.workspace.create(baseCOMP, 'rem_tox_ext')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.embody_ext.handleAddition(comp)

        self.embody_ext._removeExternalization(comp)
        self.assertEqual(comp.par.externaltox.eval(), '')

    def test_remove_tox_unlocks_readonly(self):
        """_removeExternalization should unlock externaltox readOnly."""
        comp = self.workspace.create(baseCOMP, 'rem_tox_ro')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.embody_ext.handleAddition(comp)
        comp.par.externaltox.readOnly = True

        self.embody_ext._removeExternalization(comp)
        self.assertFalse(comp.par.externaltox.readOnly)

    def test_remove_tox_resets_color(self):
        """_removeExternalization should reset operator color to default."""
        comp = self.workspace.create(baseCOMP, 'rem_tox_color')
        tox_tag = self.embody.par.Toxtag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.embody_ext.handleAddition(comp)

        self.embody_ext._removeExternalization(comp)
        default_color = (0.55, 0.55, 0.55)
        close = all(abs(a - b) < 0.02 for a, b in zip(comp.color, default_color))
        self.assertTrue(close, f'Color should reset, got {comp.color}')

    def test_remove_tox_does_not_add_tdn_tag(self):
        """REGRESSION: _removeExternalization must NOT add the other strategy tag."""
        comp = self.workspace.create(baseCOMP, 'rem_no_convert')
        tox_tag = self.embody.par.Toxtag.val
        tdn_tag = self.embody.par.Tdntag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)
        self.embody_ext.handleAddition(comp)

        self.embody_ext._removeExternalization(comp)
        self.assertNotIn(tox_tag, comp.tags)
        self.assertNotIn(tdn_tag, comp.tags)

    # =========================================================================
    # _removeExternalization — TDN
    # =========================================================================

    def test_remove_tdn_removes_tag(self):
        """_removeExternalization should remove the tdn tag from a COMP."""
        comp = self.workspace.create(baseCOMP, 'rem_tdn_tag')
        tdn_tag = self.embody.par.Tdntag.val
        self.embody_ext.applyTagToOperator(comp, tdn_tag)

        self.embody_ext._removeExternalization(comp)
        self.assertNotIn(tdn_tag, comp.tags)

    def test_remove_tdn_does_not_add_tox_tag(self):
        """REGRESSION: _removeExternalization must NOT add tox tag when removing TDN."""
        comp = self.workspace.create(baseCOMP, 'rem_tdn_no_tox')
        tox_tag = self.embody.par.Toxtag.val
        tdn_tag = self.embody.par.Tdntag.val
        self.embody_ext.applyTagToOperator(comp, tdn_tag)

        self.embody_ext._removeExternalization(comp)
        self.assertNotIn(tdn_tag, comp.tags)
        self.assertNotIn(tox_tag, comp.tags)

    def test_remove_tdn_resets_color(self):
        """_removeExternalization should reset color after TDN removal."""
        comp = self.workspace.create(baseCOMP, 'rem_tdn_color')
        tdn_tag = self.embody.par.Tdntag.val
        self.embody_ext.applyTagToOperator(comp, tdn_tag)

        self.embody_ext._removeExternalization(comp)
        default_color = (0.55, 0.55, 0.55)
        close = all(abs(a - b) < 0.02 for a, b in zip(comp.color, default_color))
        self.assertTrue(close, f'Color should reset, got {comp.color}')


class TestHandleStrategySwitch(EmbodyTestCase):

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')

    def tearDown(self):
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    def test_switch_tox_to_tdn(self):
        """HandleStrategySwitch should convert a TOX COMP to TDN."""
        comp = self.workspace.create(baseCOMP, 'switch_to_tdn')
        tox_tag = self.embody.par.Toxtag.val
        tdn_tag = self.embody.par.Tdntag.val
        self.embody_ext.applyTagToOperator(comp, tox_tag)

        self.embody_ext.HandleStrategySwitch(comp)
        self.assertNotIn(tox_tag, comp.tags)
        self.assertIn(tdn_tag, comp.tags)

    def test_switch_tdn_to_tox(self):
        """HandleStrategySwitch should convert a TDN COMP to TOX."""
        comp = self.workspace.create(baseCOMP, 'switch_to_tox')
        tox_tag = self.embody.par.Toxtag.val
        tdn_tag = self.embody.par.Tdntag.val
        self.embody_ext.applyTagToOperator(comp, tdn_tag)

        self.embody_ext.HandleStrategySwitch(comp)
        self.assertNotIn(tdn_tag, comp.tags)
        self.assertIn(tox_tag, comp.tags)


class TestDispatchTaggerButton(EmbodyTestCase):
    """Test _dispatchTaggerButton label routing logic.

    These tests verify that the dispatch correctly identifies Remove vs
    Convert vs Switch actions based on the button label text, including
    labels with Unicode prefix characters (×, ⇄).
    """

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')
        # Track whether handlers were called
        self._called = None
        self._original_remove = self.embody_ext.HandleStrategyRemove
        self._original_switch = self.embody_ext.HandleStrategySwitch
        self._original_convert = self.embody_ext.HandleDATConvert
        self._original_exiter = self.embody_ext.TagExiter

    def tearDown(self):
        # Restore original methods
        self.embody_ext.HandleStrategyRemove = self._original_remove
        self.embody_ext.HandleStrategySwitch = self._original_switch
        self.embody_ext.HandleDATConvert = self._original_convert
        self.embody_ext.TagExiter = self._original_exiter
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    def _mock_handlers(self):
        """Replace handlers with tracking stubs."""
        self.embody_ext.HandleStrategyRemove = lambda oper: setattr(self, '_called', 'remove')
        self.embody_ext.HandleStrategySwitch = lambda oper: setattr(self, '_called', 'switch')
        self.embody_ext.HandleDATConvert = lambda oper, tag: setattr(self, '_called', 'convert')
        self.embody_ext.TagExiter = lambda: None

    def test_dispatch_remove_tox_label(self):
        """Label '×  Remove tox' should route to HandleStrategyRemove."""
        self._mock_handlers()
        comp = self.workspace.create(baseCOMP, 'disp_rem_tox')
        self.embody_ext._dispatchTaggerButton(comp, 'tox', '\u00d7  Remove tox')
        self.assertEqual(self._called, 'remove')

    def test_dispatch_remove_tdn_label(self):
        """Label '×  Remove tdn' should route to HandleStrategyRemove."""
        self._mock_handlers()
        comp = self.workspace.create(baseCOMP, 'disp_rem_tdn')
        self.embody_ext._dispatchTaggerButton(comp, 'tdn', '\u00d7  Remove tdn')
        self.assertEqual(self._called, 'remove')

    def test_dispatch_convert_label(self):
        """Label '⇄  Convert to py' should route to HandleDATConvert."""
        self._mock_handlers()
        dat = self.workspace.create(textDAT, 'disp_convert')
        self.embody_ext._dispatchTaggerButton(dat, 'py', '\u21c4  Convert to py')
        self.assertEqual(self._called, 'convert')

    def test_dispatch_switch_fallback(self):
        """Unrecognized label should fall through to HandleStrategySwitch."""
        self._mock_handlers()
        comp = self.workspace.create(baseCOMP, 'disp_switch')
        self.embody_ext._dispatchTaggerButton(comp, 'tox', 'Some other action')
        self.assertEqual(self._called, 'switch')

    def test_dispatch_plain_remove_label(self):
        """Label 'Remove externalization' (no icon) should route to remove."""
        self._mock_handlers()
        comp = self.workspace.create(baseCOMP, 'disp_plain_rem')
        self.embody_ext._dispatchTaggerButton(comp, 'tox', 'Remove externalization')
        self.assertEqual(self._called, 'remove')
