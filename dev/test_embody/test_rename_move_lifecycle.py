"""
Test suite: Rename and move lifecycle — operator rename/move detection and table updates.

Tests the continuity system that tracks operators across renames and moves:
  - Rename detection: table has old path until refresh
  - updateMovedOp: updates table path, externaltox, rel_file_path
  - _findMovedOp: matches renamed operators by external file path
  - _handleMissingOperator: cleans up truly missing operators
  - Move scenarios: getOpPaths reflects new parent hierarchy
  - cleanupDuplicateRows: keeps most recent timestamp
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestRenameMoveLifecycle(EmbodyTestCase):

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
    # Rename detection
    # =========================================================================

    def test_rename_comp_table_has_old_path_before_refresh(self):
        """After renaming, the table still has the old path (stale until refresh)."""
        comp, old_path, _ = self._externalize_comp(self.workspace, 'rename_stale')
        comp.name = 'renamed_stale'

        found_old = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found_old = True
                break
        self.assertTrue(found_old, 'Table should still have old path before refresh')

    def test_rename_dat_table_has_old_path(self):
        """After renaming a DAT, the table still has the old path."""
        dat, old_path, _ = self._externalize_dat(self.workspace, 'rename_dat')
        dat.name = 'renamed_dat'

        found_old = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found_old = True
                break
        self.assertTrue(found_old, 'Table should still have old path')

    # =========================================================================
    # updateMovedOp — COMP
    # =========================================================================

    def test_updateMovedOp_updates_table_path(self):
        """updateMovedOp should update the table row path to the new path."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'move_path')
        comp.name = 'moved_path'
        new_path = comp.path

        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.updateMovedOp(comp, old_path, old_rel, ext_folder)

        found_new = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == new_path:
                found_new = True
                break
        self.assertTrue(found_new, f'Table should have new path {new_path}')

    def test_updateMovedOp_updates_externaltox(self):
        """updateMovedOp should update the externaltox to reflect the new name."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'move_ext')
        comp.name = 'moved_ext'

        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.updateMovedOp(comp, old_path, old_rel, ext_folder)

        new_ext = comp.par.externaltox.eval()
        self.assertIn('moved_ext', new_ext)

    def test_updateMovedOp_updates_rel_file_path(self):
        """updateMovedOp should update rel_file_path in the table."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'move_rel')
        comp.name = 'moved_rel'
        new_path = comp.path

        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.updateMovedOp(comp, old_path, old_rel, ext_folder)

        new_rel = self.embody_ext.Externalizations[new_path, 'rel_file_path'].val
        self.assertIn('moved_rel', new_rel)

    def test_updateMovedOp_sets_externaltox_readonly(self):
        """updateMovedOp should set externaltox to readOnly after update."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'move_ro')
        comp.name = 'moved_ro'

        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.updateMovedOp(comp, old_path, old_rel, ext_folder)

        self.assertTrue(comp.par.externaltox.readOnly)

    def test_updateMovedOp_enables_externaltox(self):
        """updateMovedOp should enable externaltox on the COMP."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'move_enable')
        comp.name = 'moved_enable'

        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.updateMovedOp(comp, old_path, old_rel, ext_folder)

        self.assertTrue(comp.par.enableexternaltox.eval())

    # =========================================================================
    # updateMovedOp — DAT
    # =========================================================================

    def test_updateMovedOp_dat_updates_file_par(self):
        """updateMovedOp for a DAT should update the file parameter."""
        dat, old_path, old_rel = self._externalize_dat(self.workspace, 'move_dat')
        dat.name = 'moved_dat'

        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.updateMovedOp(dat, old_path, old_rel, ext_folder)

        new_file = dat.par.file.eval()
        self.assertIn('moved_dat', new_file)

    def test_updateMovedOp_dat_enables_syncfile(self):
        """updateMovedOp for a DAT should enable syncfile."""
        dat, old_path, old_rel = self._externalize_dat(self.workspace, 'move_sync')
        dat.name = 'moved_sync'

        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.updateMovedOp(dat, old_path, old_rel, ext_folder)

        self.assertTrue(dat.par.syncfile.eval())

    # =========================================================================
    # updateMovedOp — error cases
    # =========================================================================

    def test_updateMovedOp_missing_old_path_logs_error(self):
        """updateMovedOp with a non-existent old path should not crash."""
        comp = self.workspace.create(baseCOMP, 'no_old')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        ext_folder = self.embody_ext.ExternalizationsFolder
        # Should not raise — old path doesn't exist in table
        self.embody_ext.updateMovedOp(comp, '/nonexistent/old', 'fake/path.tox', ext_folder)

    # =========================================================================
    # _findMovedOp
    # =========================================================================

    def test_findMovedOp_finds_renamed_comp(self):
        """_findMovedOp should find a COMP that was renamed by matching externaltox."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'find_comp')
        comp.name = 'found_comp'

        processed = set()
        ext_folder = self.embody_ext.ExternalizationsFolder
        found = self.embody_ext._findMovedOp(old_path, old_rel, ext_folder, processed)
        self.assertTrue(found, 'Should find the renamed COMP')

    def test_findMovedOp_finds_renamed_dat(self):
        """_findMovedOp should find a DAT that was renamed by matching file parameter."""
        dat, old_path, old_rel = self._externalize_dat(self.workspace, 'find_dat')
        dat.name = 'found_dat'

        processed = set()
        ext_folder = self.embody_ext.ExternalizationsFolder
        found = self.embody_ext._findMovedOp(old_path, old_rel, ext_folder, processed)
        self.assertTrue(found, 'Should find the renamed DAT')

    def test_findMovedOp_returns_false_when_truly_missing(self):
        """_findMovedOp should return False when no operator has the file path."""
        processed = set()
        ext_folder = self.embody_ext.ExternalizationsFolder
        found = self.embody_ext._findMovedOp(
            '/nonexistent/path', 'nonexistent/file.tox', ext_folder, processed
        )
        self.assertFalse(found, 'Should return False for truly missing operator')

    # =========================================================================
    # _handleMissingOperator
    # =========================================================================

    def test_handleMissingOperator_removes_from_table(self):
        """_handleMissingOperator should remove the stale row from the table."""
        comp, old_path, old_rel = self._externalize_comp(self.workspace, 'missing_comp')
        comp.destroy()

        self.embody_ext._handleMissingOperator(old_path, old_rel)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == old_path:
                found = True
                break
        self.assertFalse(found, 'Stale row should be removed')

    # =========================================================================
    # Move scenarios (reparenting)
    # =========================================================================

    def test_getOpPaths_reflects_new_parent_hierarchy(self):
        """getOpPaths should generate paths based on current parent hierarchy."""
        parent1 = self.workspace.create(baseCOMP, 'parent1')
        parent2 = self.workspace.create(baseCOMP, 'parent2')

        comp1 = parent1.create(baseCOMP, 'child')
        comp2 = parent2.create(baseCOMP, 'child')

        tox_tag = self.embody.par.Toxtag.val
        comp1.tags.add(tox_tag)
        comp2.tags.add(tox_tag)

        _, _, _, rel1 = self.embody_ext.getOpPaths(comp1)
        _, _, _, rel2 = self.embody_ext.getOpPaths(comp2)

        self.assertIn('parent1', rel1)
        self.assertIn('parent2', rel2)
        self.assertNotEqual(rel1, rel2)

    def test_copy_to_different_parent_changes_path(self):
        """Copying a COMP to a different parent should produce a different path."""
        source = self.workspace.create(baseCOMP, 'source')
        dest = self.workspace.create(baseCOMP, 'dest')
        comp = source.create(baseCOMP, 'movable')

        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)
        self.embody_ext.handleAddition(comp)

        old_path = comp.path
        moved = dest.copy(comp, name='movable')
        self.assertNotEqual(old_path, moved.path)
        self.assertIn('dest', moved.path)

    # =========================================================================
    # cleanupDuplicateRows
    # =========================================================================

    def test_cleanupDuplicateRows_keeps_most_recent(self):
        """cleanupDuplicateRows should keep the row with the most recent timestamp."""
        comp, _, _ = self._externalize_comp(self.workspace, 'dup_comp')

        # Manually insert a duplicate row with an older timestamp
        self.embody_ext.Externalizations.appendRow([
            comp.path, comp.type, 'old/path.tox',
            '2020-01-01 00:00:00 UTC', '', '', ''
        ])

        # Count rows before cleanup
        count_before = 0
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                count_before += 1
        self.assertEqual(count_before, 2, 'Should have 2 rows before cleanup')

        self.embody_ext.cleanupDuplicateRows(comp.path)

        count_after = 0
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                count_after += 1
        self.assertEqual(count_after, 1, 'Should have 1 row after cleanup')

    def test_cleanupDuplicateRows_no_duplicates_noop(self):
        """cleanupDuplicateRows with no duplicates should be a no-op."""
        comp, _, _ = self._externalize_comp(self.workspace, 'no_dup')

        initial_rows = self.embody_ext.Externalizations.numRows
        self.embody_ext.cleanupDuplicateRows(comp.path)
        self.assertEqual(self.embody_ext.Externalizations.numRows, initial_rows)
