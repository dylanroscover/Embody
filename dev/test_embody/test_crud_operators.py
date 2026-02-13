"""
Test suite: CRUD operations — handleAddition/handleSubtraction end-to-end.

Tests the full externalization pipeline: adding operators to tracking,
removing them, table management, path generation, and edge cases.
Complements test_externalization.py which covers lower-level setup methods.
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestCRUDOperators(EmbodyTestCase):

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
    # handleAddition — COMP
    # =========================================================================

    def test_handleAddition_comp_adds_table_row(self):
        """handleAddition should add a row to the Externalizations table."""
        comp = self.workspace.create(baseCOMP, 'add_row')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        initial_rows = self.embody_ext.Externalizations.numRows
        self.embody_ext.handleAddition(comp)

        self.assertGreater(self.embody_ext.Externalizations.numRows, initial_rows)

    def test_handleAddition_comp_sets_externaltox(self):
        """handleAddition should set the externaltox parameter."""
        comp = self.workspace.create(baseCOMP, 'ext_tox')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        ext_path = comp.par.externaltox.eval()
        self.assertTrue(len(ext_path) > 0, 'externaltox should be set')
        self.assertIn('ext_tox', ext_path)

    def test_handleAddition_comp_sets_readonly(self):
        """handleAddition should set externaltox to readOnly."""
        comp = self.workspace.create(baseCOMP, 'readonly')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        self.assertTrue(comp.par.externaltox.readOnly)

    def test_handleAddition_comp_enables_externaltox(self):
        """handleAddition should enable externaltox on the COMP."""
        comp = self.workspace.create(baseCOMP, 'enable_ext')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        self.assertTrue(comp.par.enableexternaltox.eval())

    def test_handleAddition_comp_table_has_correct_path(self):
        """Table row should have the correct operator path."""
        comp = self.workspace.create(baseCOMP, 'correct_path')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                found = True
                break
        self.assertTrue(found, f'Table should have row with path {comp.path}')

    def test_handleAddition_comp_table_has_correct_type(self):
        """Table row should have the correct operator type."""
        comp = self.workspace.create(baseCOMP, 'correct_type')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                op_type = self.embody_ext.Externalizations[i, 'type'].val
                self.assertEqual(op_type, comp.type)
                return
        self.assertTrue(False, 'Row not found')

    def test_handleAddition_comp_rel_file_path_is_tox(self):
        """Table row rel_file_path should end with .tox."""
        comp = self.workspace.create(baseCOMP, 'tox_ext')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                rel = self.embody_ext.Externalizations[i, 'rel_file_path'].val
                self.assertEndsWith(rel, '.tox')
                return
        self.assertTrue(False, 'Row not found')

    def test_handleAddition_comp_no_backslashes(self):
        """rel_file_path should use forward slashes only."""
        comp = self.workspace.create(baseCOMP, 'no_backslash')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                rel = self.embody_ext.Externalizations[i, 'rel_file_path'].val
                self.assertNotIn('\\', rel)
                return
        self.assertTrue(False, 'Row not found')

    def test_handleAddition_comp_build_params(self):
        """handleAddition should set up Build parameters on the COMP."""
        comp = self.workspace.create(baseCOMP, 'build_params')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)

        self.assertTrue(hasattr(comp.par, 'Build'), 'Should have Build parameter')

    # =========================================================================
    # handleAddition — DAT
    # =========================================================================

    def test_handleAddition_dat_sets_file(self):
        """handleAddition should set the file parameter on a DAT."""
        dat = self.workspace.create(textDAT, 'add_dat')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)

        self.embody_ext.handleAddition(dat)

        file_path = dat.par.file.eval()
        self.assertTrue(len(file_path) > 0, 'file should be set')
        self.assertIn('add_dat', file_path)

    def test_handleAddition_dat_sets_readonly(self):
        """handleAddition should set file to readOnly on a DAT."""
        dat = self.workspace.create(textDAT, 'ro_dat')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)

        self.embody_ext.handleAddition(dat)

        self.assertTrue(dat.par.file.readOnly)

    def test_handleAddition_dat_adds_table_row(self):
        """handleAddition should add a table row for a DAT."""
        dat = self.workspace.create(textDAT, 'dat_row')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)

        self.embody_ext.handleAddition(dat)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == dat.path:
                found = True
                break
        self.assertTrue(found, 'Table should have row for DAT')

    def test_handleAddition_dat_rel_path_has_py_extension(self):
        """DAT rel_file_path should end with .py for py-tagged DATs."""
        dat = self.workspace.create(textDAT, 'py_ext')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)

        self.embody_ext.handleAddition(dat)

        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == dat.path:
                rel = self.embody_ext.Externalizations[i, 'rel_file_path'].val
                self.assertEndsWith(rel, '.py')
                return
        self.assertTrue(False, 'Row not found')

    # =========================================================================
    # handleAddition — idempotency
    # =========================================================================

    def test_handleAddition_idempotent_no_duplicate_rows(self):
        """Calling handleAddition twice should not create duplicate rows."""
        comp = self.workspace.create(baseCOMP, 'idempotent')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)

        self.embody_ext.handleAddition(comp)
        self.embody_ext.handleAddition(comp)

        count = 0
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                count += 1
        self.assertEqual(count, 1, 'Should have exactly one row')

    # =========================================================================
    # handleSubtraction
    # =========================================================================

    def test_handleSubtraction_comp_removes_row(self):
        """handleSubtraction should remove the table row for a COMP."""
        comp = self.workspace.create(baseCOMP, 'sub_comp')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)
        self.embody_ext.handleAddition(comp)

        self.embody_ext.handleSubtraction(comp)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == comp.path:
                found = True
                break
        self.assertFalse(found, 'Row should be removed')

    def test_handleSubtraction_comp_unlocks_readonly(self):
        """handleSubtraction should unlock externaltox readOnly."""
        comp = self.workspace.create(baseCOMP, 'sub_ro')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)
        self.embody_ext.handleAddition(comp)

        self.embody_ext.handleSubtraction(comp)

        self.assertFalse(comp.par.externaltox.readOnly)

    def test_handleSubtraction_dat_removes_row(self):
        """handleSubtraction should remove the table row for a DAT."""
        dat = self.workspace.create(textDAT, 'sub_dat')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)
        self.embody_ext.handleAddition(dat)

        self.embody_ext.handleSubtraction(dat)

        found = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == dat.path:
                found = True
                break
        self.assertFalse(found, 'Row should be removed')

    def test_handleSubtraction_dat_unlocks_readonly(self):
        """handleSubtraction should unlock file readOnly on a DAT."""
        dat = self.workspace.create(textDAT, 'sub_dat_ro')
        py_tag = self.embody.par.Pytag.val
        dat.tags.add(py_tag)
        self.embody_ext.handleAddition(dat)

        self.embody_ext.handleSubtraction(dat)

        self.assertFalse(dat.par.file.readOnly)

    # =========================================================================
    # Multiple operators
    # =========================================================================

    def test_multiple_comps_unique_paths(self):
        """Three COMPs should each get unique rel_file_path values."""
        comps = []
        tox_tag = self.embody.par.Toxtag.val
        for name in ['comp_a', 'comp_b', 'comp_c']:
            c = self.workspace.create(baseCOMP, name)
            c.tags.add(tox_tag)
            self.embody_ext.handleAddition(c)
            comps.append(c)

        paths = set()
        for c in comps:
            for i in range(1, self.embody_ext.Externalizations.numRows):
                if self.embody_ext.Externalizations[i, 'path'].val == c.path:
                    paths.add(self.embody_ext.Externalizations[i, 'rel_file_path'].val)
                    break

        self.assertLen(paths, 3)

    def test_mixed_comp_and_dat(self):
        """A COMP and a DAT externalized together should both get table rows."""
        comp = self.workspace.create(baseCOMP, 'mixed_comp')
        dat = self.workspace.create(textDAT, 'mixed_dat')

        tox_tag = self.embody.par.Toxtag.val
        py_tag = self.embody.par.Pytag.val
        comp.tags.add(tox_tag)
        dat.tags.add(py_tag)

        self.embody_ext.handleAddition(comp)
        self.embody_ext.handleAddition(dat)

        found_comp = False
        found_dat = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            p = self.embody_ext.Externalizations[i, 'path'].val
            if p == comp.path:
                found_comp = True
            elif p == dat.path:
                found_dat = True

        self.assertTrue(found_comp, 'COMP should be in table')
        self.assertTrue(found_dat, 'DAT should be in table')

    # =========================================================================
    # Nesting
    # =========================================================================

    def test_nested_comp_hierarchy_in_path(self):
        """A nested COMP's rel_file_path should reflect parent hierarchy."""
        parent = self.workspace.create(baseCOMP, 'outer')
        child = parent.create(baseCOMP, 'inner')
        tox_tag = self.embody.par.Toxtag.val
        child.tags.add(tox_tag)

        self.embody_ext.handleAddition(child)

        for i in range(1, self.embody_ext.Externalizations.numRows):
            if self.embody_ext.Externalizations[i, 'path'].val == child.path:
                rel = self.embody_ext.Externalizations[i, 'rel_file_path'].val
                self.assertIn('outer', rel)
                self.assertIn('inner', rel)
                return
        self.assertTrue(False, 'Row not found')
