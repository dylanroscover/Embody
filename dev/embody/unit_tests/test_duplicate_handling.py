"""
Test suite: Duplicate row handling in EmbodyExt.

Tests cleanupDuplicateRows, cleanupAllDuplicateRows.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestDuplicateHandling(EmbodyTestCase):

    # --- cleanupAllDuplicateRows ---

    def test_cleanupAllDuplicateRows_runs_without_error(self):
        # Should not raise on the current externalizations table
        self.embody_ext.cleanupAllDuplicateRows()

    # --- cleanupDuplicateRows ---

    def test_cleanupDuplicateRows_nonexistent_path_returns_none(self):
        result = self.embody_ext.cleanupDuplicateRows('/nonexistent/path')
        # When path is not found, should return None or 0
        if result is not None:
            self.assertEqual(result, 0)

    def test_cleanupDuplicateRows_existing_path_no_duplicates(self):
        # Get any existing externalized op path
        table = self.embody_ext.Externalizations
        if not table or table.numRows <= 1:
            self.skip('No externalizations to check')
        existing_path = table[1, 'path'].val
        result = self.embody_ext.cleanupDuplicateRows(existing_path)
        # Should return 0 (no duplicates to clean) or None
        if result is not None:
            self.assertGreaterEqual(result, 0)

    # --- Table integrity after cleanup ---

    def test_cleanup_preserves_table_row_count(self):
        table = self.embody_ext.Externalizations
        if not table:
            self.skip('No externalizations table')
        initial_rows = table.numRows
        self.embody_ext.cleanupAllDuplicateRows()
        # Row count should be same or less (never more)
        self.assertLessEqual(table.numRows, initial_rows)
