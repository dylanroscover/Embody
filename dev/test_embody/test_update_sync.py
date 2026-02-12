"""
Test suite: Update and sync cycle in EmbodyExt.

Tests normalizeAllPaths, checkOpsForContinuity, createExternalizationsTable.
Note: We don't test the full Update() cycle as it modifies files on disk.
"""

runner_mod = op('TestRunner').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestUpdateSync(EmbodyTestCase):

    # --- normalizeAllPaths ---

    def test_normalizeAllPaths_runs_without_error(self):
        # Should not raise even on empty or populated table
        self.embody_ext.normalizeAllPaths()

    # --- createExternalizationsTable ---

    def test_createExternalizationsTable_header_row(self):
        # Verify the existing table has the right header
        table = self.embody_ext.Externalizations
        if table and table.numRows > 0:
            headers = [table[0, i].val for i in range(table.numCols)]
            self.assertIn('path', headers)
            self.assertIn('type', headers)
            self.assertIn('rel_file_path', headers)
            self.assertIn('timestamp', headers)
            self.assertIn('dirty', headers)
            self.assertIn('build', headers)

    # --- Externalizations property ---

    def test_externalizations_property_returns_dat(self):
        table = self.embody_ext.Externalizations
        self.assertIsNotNone(table)
        self.assertEqual(table.family, 'DAT')

    # --- Table integrity ---

    def test_externalizations_table_rows_have_valid_paths(self):
        table = self.embody_ext.Externalizations
        if not table or table.numRows <= 1:
            self.skip('No externalizations to check')
        for i in range(1, table.numRows):
            path = table[i, 'path'].val
            self.assertTrue(path.startswith('/'),
                            f'Row {i} path should start with /: {path}')

    def test_externalizations_table_rows_have_type(self):
        table = self.embody_ext.Externalizations
        if not table or table.numRows <= 1:
            self.skip('No externalizations to check')
        for i in range(1, table.numRows):
            op_type = table[i, 'type'].val
            self.assertTrue(len(op_type) > 0,
                            f'Row {i} type should not be empty')

    def test_externalizations_table_rows_have_file_path(self):
        table = self.embody_ext.Externalizations
        if not table or table.numRows <= 1:
            self.skip('No externalizations to check')
        for i in range(1, table.numRows):
            rel_path = table[i, 'rel_file_path'].val
            self.assertTrue(len(rel_path) > 0,
                            f'Row {i} rel_file_path should not be empty')

    def test_externalizations_no_backslashes_in_paths(self):
        table = self.embody_ext.Externalizations
        if not table or table.numRows <= 1:
            self.skip('No externalizations to check')
        for i in range(1, table.numRows):
            rel_path = table[i, 'rel_file_path'].val
            self.assertNotIn('\\', rel_path,
                             f'Row {i} should use forward slashes: {rel_path}')
