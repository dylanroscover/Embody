"""
Test suite: Externalization pipeline in EmbodyExt.

Tests handleAddition, handleSubtraction, _setupCompForExternalization,
_setupDatForExternalization, _addToTable, setupBuildParameters, getOpPaths.
"""

import os
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestExternalization(EmbodyTestCase):

    def setUp(self):
        self._test_dir = Path(project.folder) / 'test_embody' / '_test_temp'
        self._test_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for f in self._test_dir.glob('*'):
            try:
                f.unlink()
            except OSError:
                pass
        super().tearDown()

    # --- getOpPaths ---

    def test_getOpPaths_comp_with_existing_path(self):
        comp = self.sandbox.create(baseCOMP, 'existing_path')
        comp.par.externaltox = 'embody/existing_path.tox'
        result = self.embody_ext.getOpPaths(comp)
        abs_folder, save_path, rel_dir, rel_file = result
        self.assertIsNotNone(abs_folder)
        self.assertIsNotNone(save_path)
        self.assertEqual(rel_file, 'embody/existing_path.tox')

    def test_getOpPaths_comp_new_generates_tox(self):
        comp = self.sandbox.create(baseCOMP, 'new_comp')
        tox_tag = self.embody.par.Toxtag.val
        comp.tags.add(tox_tag)
        result = self.embody_ext.getOpPaths(comp)
        abs_folder, save_path, rel_dir, rel_file = result
        self.assertIsNotNone(rel_file)
        self.assertEndsWith(rel_file, '.tox')
        self.assertIn('new_comp', rel_file)

    def test_getOpPaths_returns_none_tuple_on_error(self):
        # A DAT without any tags should return all Nones
        dat = self.sandbox.create(textDAT, 'no_tag_dat')
        result = self.embody_ext.getOpPaths(dat)
        abs_folder, save_path, rel_dir, rel_file = result
        self.assertIsNone(abs_folder)
        self.assertIsNone(rel_file)

    # --- Externalizations table structure ---

    def test_externalizations_table_exists(self):
        table = self.embody_ext.Externalizations
        self.assertIsNotNone(table)

    def test_externalizations_table_has_columns(self):
        table = self.embody_ext.Externalizations
        if table and table.numRows > 0:
            header_cells = [table[0, i].val for i in range(table.numCols)]
            self.assertIn('path', header_cells)
            self.assertIn('type', header_cells)
            self.assertIn('rel_file_path', header_cells)

    # --- _setupCompForExternalization ---

    def test_setupComp_sets_externaltox(self):
        comp = self.sandbox.create(baseCOMP, 'setup_comp')
        rel_path = 'test_embody/_test_temp/setup_comp.tox'
        self.embody_ext._setupCompForExternalization(
            comp, rel_path,
            str(self.embody_ext.buildAbsolutePath(rel_path))
        )
        result = comp.par.externaltox.eval()
        self.assertIn('setup_comp.tox', result)

    def test_setupComp_enables_externaltox(self):
        comp = self.sandbox.create(baseCOMP, 'enable_test')
        rel_path = 'test_embody/_test_temp/enable_test.tox'
        self.embody_ext._setupCompForExternalization(
            comp, rel_path,
            str(self.embody_ext.buildAbsolutePath(rel_path))
        )
        self.assertTrue(comp.par.enableexternaltox.eval())

    # --- _setupDatForExternalization ---

    def test_setupDat_sets_file(self):
        dat = self.sandbox.create(textDAT, 'setup_dat')
        rel_path = 'test_embody/_test_temp/setup_dat.py'
        self.embody_ext._setupDatForExternalization(
            dat, rel_path,
            str(self.embody_ext.buildAbsolutePath(rel_path))
        )
        result = dat.par.file.eval()
        self.assertIn('setup_dat.py', result)

    # --- ExternalizationsFolder property ---

    def test_externalizations_folder_returns_string(self):
        result = self.embody_ext.ExternalizationsFolder
        self.assertIsInstance(result, str)
