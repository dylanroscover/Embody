"""
Test suite: Deletion and cleanup — RemoveListerRow, stale rows, file references.

Tests operator deletion, stale row handling, and the RemoveListerRow method:
  - Delete comp leaves stale table row
  - _handleMissingOperator cleans stale row
  - RemoveListerRow: removes tags, clears params, resets color, removes from tracker
  - RemoveListerRow: clone tag preserves file
  - _checkFileReferences: shared vs unique files
  - RemoveListerRow on already-destroyed op doesn't crash
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestDeleteCleanup(EmbodyTestCase):

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

    # --- Helper ---

    def _externalize_comp(self, parent, name):
        """Create a COMP, tag it, and externalize. Returns (comp, old_path, old_rel)."""
        comp = parent.create(baseCOMP, name)
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)
        self.embody_ext.handleAddition(comp)
        old_path = comp.path
        old_rel = self.embody_ext.normalizePath(
            self.embody_ext.Externalizations[comp.path, 'rel_file_path'].val
        )
        return comp, old_path, old_rel

    def _externalize_dat(self, parent, name):
        """Create a textDAT, tag it, and externalize. Returns (dat, old_path, old_rel)."""
        dat = parent.create(textDAT, name)
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)
        self.embody_ext.handleAddition(dat)
        old_path = dat.path
        old_rel = self.embody_ext.normalizePath(
            self.embody_ext.Externalizations[dat.path, 'rel_file_path'].val
        )
        return dat, old_path, old_rel

    # =========================================================================
    # Delete leaves stale row
    # =========================================================================

    def test_delete_comp_leaves_stale_row(self):
        """Destroying a COMP should leave a stale row in the table."""
        comp, old_path, _ = self._externalize_comp(self.workspace, 'del_stale')
        comp.destroy()

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found = True
                break
        self.assertTrue(found, 'Stale row should remain after destroy')

    def test_delete_dat_leaves_stale_row(self):
        """Destroying a DAT should leave a stale row in the table."""
        dat, old_path, _ = self._externalize_dat(self.workspace, 'del_dat')
        dat.destroy()

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found = True
                break
        self.assertTrue(found, 'Stale row should remain after destroy')

    # =========================================================================
    # _handleMissingOperator
    # =========================================================================

    def test_handleMissingOperator_cleans_stale_comp(self):
        """_handleMissingOperator should remove stale COMP row."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'missing_comp')
        comp.destroy()

        self.embody_ext._handleMissingOperator(old_path, old_rel)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found = True
                break
        self.assertFalse(found, 'Row should be cleaned up')

    def test_handleMissingOperator_cleans_stale_dat(self):
        """_handleMissingOperator should remove stale DAT row."""
        dat, old_path, old_rel = self._externalize_dat(self.workspace, 'missing_dat')
        dat.destroy()

        self.embody_ext._handleMissingOperator(old_path, old_rel)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found = True
                break
        self.assertFalse(found, 'Row should be cleaned up')

    # =========================================================================
    # RemoveListerRow — COMP
    # =========================================================================

    def test_removeListerRow_comp_removes_tag(self):
        """RemoveListerRow should remove the externalization tag from a COMP."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'rm_tag')
        tox_tag = self.embody.par.Toxtag.val

        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertNotIn(tox_tag, comp.tags)

    def test_removeListerRow_comp_clears_externaltox(self):
        """RemoveListerRow should clear the externaltox parameter."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'rm_ext')

        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertEqual(comp.par.externaltox.eval(), '')

    def test_removeListerRow_comp_resets_color(self):
        """RemoveListerRow should reset the operator color to default."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'rm_color')

        self.embody_ext.RemoveListerRow(old_path, old_rel)

        default_color = (0.55, 0.55, 0.55)
        color = comp.color
        close = all(abs(a - b) < 0.02 for a, b in zip(color, default_color))
        self.assertTrue(close, f'Color should reset to default, got {color}')

    def test_removeListerRow_comp_removes_table_row(self):
        """RemoveListerRow should remove the row from the Externalizations table."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'rm_row')

        self.embody_ext.RemoveListerRow(old_path, old_rel)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found = True
                break
        self.assertFalse(found, 'Row should be removed from table')

    def test_removeListerRow_comp_unlocks_readonly(self):
        """RemoveListerRow should unlock the readOnly flag on externaltox."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'rm_ro')

        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertFalse(comp.par.externaltox.readOnly)

    # =========================================================================
    # RemoveListerRow — DAT
    # =========================================================================

    def test_removeListerRow_dat_removes_tag(self):
        """RemoveListerRow should remove the externalization tag from a DAT."""
        dat, old_path, old_rel = self._externalize_dat(self.workspace, 'rm_dat_tag')
        py_tag = self.embody.par.Pytag.val

        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertNotIn(py_tag, dat.tags)

    def test_removeListerRow_dat_clears_file(self):
        """RemoveListerRow should clear the file parameter on a DAT."""
        dat, old_path, old_rel = self._externalize_dat(self.workspace, 'rm_dat_file')

        self.embody_ext.RemoveListerRow(old_path, old_rel)
        self.assertEqual(dat.par.file.eval(), '')

    def test_removeListerRow_dat_removes_table_row(self):
        """RemoveListerRow should remove the DAT row from the table."""
        dat, old_path, old_rel = self._externalize_dat(self.workspace, 'rm_dat_row')

        self.embody_ext.RemoveListerRow(old_path, old_rel)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found = True
                break
        self.assertFalse(found, 'Row should be removed from table')

    # =========================================================================
    # RemoveListerRow — edge cases
    # =========================================================================

    def test_removeListerRow_destroyed_op_no_crash(self):
        """RemoveListerRow on an already-destroyed operator should not crash."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'rm_destroyed')
        comp.destroy()

        # Should not raise
        self.embody_ext.RemoveListerRow(old_path, old_rel)

    def test_removeListerRow_nonexistent_path_no_crash(self):
        """RemoveListerRow with a path not in the table should not crash."""
        # Should not raise
        self.embody_ext.RemoveListerRow('/nonexistent/path', 'fake/file.tox')

    # =========================================================================
    # _checkFileReferences
    # =========================================================================

    def test_checkFileReferences_unique_file_returns_false(self):
        """_checkFileReferences should return False when file has no other references."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'unique_file')

        shared = self.embody_ext._checkFileReferences(old_path, old_rel)
        # Only one op references this file, so it's not shared
        self.assertFalse(shared)

    def test_checkFileReferences_shared_file_returns_true(self):
        """_checkFileReferences should return True when multiple ops reference same file."""
        comp1, _, old_rel = self._externalize_comp(self.workspace, 'shared1')

        # Create a second COMP whose externaltox points to the same file
        comp2 = self.workspace.create(baseCOMP, 'shared2')
        comp2.par.externaltox = comp1.par.externaltox.eval()

        shared = self.embody_ext._checkFileReferences(comp1.path, old_rel)
        self.assertTrue(shared)
