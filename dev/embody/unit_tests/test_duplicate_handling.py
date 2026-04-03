"""
Test suite: Duplicate path handling in EmbodyExt.

Tests cleanupDuplicateRows, cleanupAllDuplicateRows, checkForDuplicates,
_buildPathGroups, and the clone-resolution helpers.
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

    # --- _buildPathGroups ---

    def test_build_path_groups_returns_dict(self):
        result = self.embody_ext._buildPathGroups()
        self.assertIsInstance(result, dict)

    def test_build_path_groups_values_are_lists(self):
        result = self.embody_ext._buildPathGroups()
        for path, ops in result.items():
            self.assertIsInstance(ops, list)
            self.assertGreater(len(ops), 0)

    def test_build_path_groups_skips_untagged_ops(self):
        comp = self.sandbox.create(baseCOMP, 'untagged')
        comp.par.externaltox = 'test/untagged.tox'
        result = self.embody_ext._buildPathGroups()
        # The untagged comp should not appear in any group
        for ops in result.values():
            for o in ops:
                self.assertNotEqual(o.path, comp.path)

    # --- _resolveClonesByCloningAPI ---

    def test_resolve_clones_returns_false_for_dats(self):
        dat1 = self.sandbox.create(textDAT, 'dat_a')
        dat2 = self.sandbox.create(textDAT, 'dat_b')
        result = self.embody_ext._resolveClonesByCloningAPI([dat1, dat2])
        self.assertFalse(result)

    def test_resolve_clones_returns_false_for_mixed(self):
        comp = self.sandbox.create(baseCOMP, 'mixed_comp')
        dat = self.sandbox.create(textDAT, 'mixed_dat')
        result = self.embody_ext._resolveClonesByCloningAPI([comp, dat])
        self.assertFalse(result)

    def test_resolve_clones_returns_false_without_clone_relationship(self):
        comp1 = self.sandbox.create(baseCOMP, 'no_clone_a')
        comp2 = self.sandbox.create(baseCOMP, 'no_clone_b')
        result = self.embody_ext._resolveClonesByCloningAPI([comp1, comp2])
        self.assertFalse(result)


class TestReplicantHandling(EmbodyTestCase):
    """Tests for replicant filtering in duplicate detection."""

    def test_resolve_replicants_returns_false_for_non_replicants(self):
        """_resolveReplicants returns False when no ops are replicants."""
        comp1 = self.sandbox.create(baseCOMP, 'non_rep_a')
        comp2 = self.sandbox.create(baseCOMP, 'non_rep_b')
        result = self.embody_ext._resolveReplicants([comp1, comp2])
        self.assertFalse(result)

    def test_resolve_replicants_returns_false_for_dats(self):
        """_resolveReplicants returns False for DATs (no replicator)."""
        dat1 = self.sandbox.create(textDAT, 'rep_dat_a')
        dat2 = self.sandbox.create(textDAT, 'rep_dat_b')
        result = self.embody_ext._resolveReplicants([dat1, dat2])
        self.assertFalse(result)

    def test_is_replicant_false_for_regular_comp(self):
        """isReplicant returns False for a regular COMP."""
        comp = self.sandbox.create(baseCOMP, 'regular_comp')
        self.assertFalse(self.embody_ext.isReplicant(comp))

    def test_build_path_groups_skips_replicants(self):
        """_buildPathGroups must not include replicant COMPs."""
        # Create a replicator with a tagged master
        host = self.sandbox.create(baseCOMP, 'rep_host')
        master = host.create(baseCOMP, 'rep_master')
        tox_tag = self.embody.par.Toxtag.val
        master.tags.add(tox_tag)
        master.par.externaltox = 'test/replicated.tox'
        # Drive the replicator with a table
        table = host.create(tableDAT, 'rep_table')
        table.appendRow(['name'])
        for i in range(5):
            table.appendRow([f'item{i}'])
        replicator = host.create(replicatorCOMP, 'rep1')
        replicator.par.template = table.name
        replicator.par.master = master.name
        replicator.cook(force=True)

        # Verify replicants were created and are detected
        replicants = [
            c for c in host.children
            if c is not master and c is not replicator
            and c.family == 'COMP' and c.type != 'tableDAT']
        # Filter to only actual replicants (not the table DAT)
        replicants = [c for c in replicants if self.embody_ext.isReplicant(c)]
        if not replicants:
            self.skip('Replicator did not produce replicants (timing?)')

        result = self.embody_ext._buildPathGroups()
        # None of the replicants should appear in the groups
        for ops in result.values():
            for o in ops:
                self.assertFalse(
                    self.embody_ext.isReplicant(o),
                    f'Replicant {o.path} should not appear in path groups')


class TestCheckForDuplicates(EmbodyTestCase):
    """Tests for checkForDuplicates and its dialog-driven helpers."""

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'dup_workspace')
        self._prev_detect = self.embody.par.Detectduplicatepaths.eval()
        self.embody.par.Detectduplicatepaths = True

    def tearDown(self):
        # Clean up table rows added during tests
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        self.embody.par.Detectduplicatepaths = self._prev_detect
        # Clean stored test responses
        self.embody.unstore('_smoke_test_responses')
        super().tearDown()

    def _make_tagged_dats(self, names, shared_path='test/shared.py'):
        """Create multiple tagged DATs pointing to the same file."""
        py_tag = self.embody.par.Pytag.val
        dats = []
        for name in names:
            dat = self.workspace.create(textDAT, name)
            dat.tags.add(py_tag)
            dat.par.file = shared_path
            dat.par.syncfile = True
            dats.append(dat)
        return dats

    def test_group_level_clone_tag_skips_entire_group(self):
        dats = self._make_tagged_dats(['skip_a', 'skip_b', 'skip_c'])
        # Pre-tag one op with 'clone' — entire group should be skipped
        dats[1].tags.add('clone')
        # Seed a response that should NOT be consumed
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 1})
        self.embody_ext.checkForDuplicates()
        # The other ops should NOT have been tagged
        self.assertNotIn('clone', dats[0].tags)
        self.assertNotIn('clone', dats[2].tags)
        # Response should still be there (unconsumed)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNotNone(responses)

    def test_user_selects_master_tags_others(self):
        dats = self._make_tagged_dats(['sel_master', 'sel_clone'])
        # User picks button 1 (first op in group = master).
        # findChildren order is not guaranteed, so check that exactly
        # one op got the clone tag and the other did not.
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 1})
        self.embody_ext.checkForDuplicates()
        clone_count = sum(1 for d in dats if 'clone' in d.tags)
        self.assertEqual(clone_count, 1, 'Exactly one op should be tagged clone')

    def test_dismiss_does_not_tag_anyone(self):
        dats = self._make_tagged_dats(['dismiss_a', 'dismiss_b'])
        # User picks button 0 (Dismiss)
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 0})
        self.embody_ext.checkForDuplicates()
        self.assertNotIn('clone', dats[0].tags)
        self.assertNotIn('clone', dats[1].tags)

    def test_three_ops_user_selects_middle(self):
        dats = self._make_tagged_dats(['tri_a', 'tri_b', 'tri_c'])
        # User picks button 2 (second op = master)
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 2})
        self.embody_ext.checkForDuplicates()
        # tri_b is master — others should be clones
        self.assertIn('clone', dats[0].tags)
        self.assertNotIn('clone', dats[1].tags)
        self.assertIn('clone', dats[2].tags)

    def test_reference_adds_table_row_with_strategy(self):
        dats = self._make_tagged_dats(['strat_a', 'strat_b'])
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 1})
        self.embody_ext.checkForDuplicates()
        # The clone (strat_b) should have a table row
        table = self.embody_ext.Externalizations
        has_strategy = table[0, 'strategy'] is not None
        if has_strategy:
            for i in range(1, table.numRows):
                if table[i, 'path'].val == dats[1].path:
                    strategy = table[i, 'strategy'].val
                    self.assertTrue(len(strategy) > 0,
                                    'strategy column should not be empty')
                    return
            self.fail(f'No table row found for {dats[1].path}')
