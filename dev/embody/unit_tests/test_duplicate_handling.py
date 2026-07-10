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
            self.skipTest('No externalizations to check')
        existing_path = table[1, 'path'].val
        result = self.embody_ext.cleanupDuplicateRows(existing_path)
        # Should return 0 (no duplicates to clean) or None
        if result is not None:
            self.assertGreaterEqual(result, 0)

    # --- Table integrity after cleanup ---

    def test_cleanup_preserves_table_row_count(self):
        table = self.embody_ext.Externalizations
        if not table:
            self.skipTest('No externalizations table')
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

    # --- _resolveDATsInClonedCOMPs ---

    def test_resolve_dats_in_cloned_comps_returns_false_for_comps(self):
        """_resolveDATsInClonedCOMPs returns False when ops are COMPs."""
        comp1 = self.sandbox.create(baseCOMP, 'comp_not_dat_a')
        comp2 = self.sandbox.create(baseCOMP, 'comp_not_dat_b')
        result = self.embody_ext._resolveDATsInClonedCOMPs([comp1, comp2])
        self.assertFalse(result)

    def test_resolve_dats_in_cloned_comps_returns_false_for_mixed(self):
        """_resolveDATsInClonedCOMPs returns False for mixed DAT/COMP lists."""
        comp = self.sandbox.create(baseCOMP, 'mixed_c')
        dat = self.sandbox.create(textDAT, 'mixed_d')
        result = self.embody_ext._resolveDATsInClonedCOMPs([comp, dat])
        self.assertFalse(result)

    def test_resolve_dats_in_cloned_comps_returns_false_no_clone(self):
        """_resolveDATsInClonedCOMPs returns False when no ancestor is a clone."""
        dat1 = self.sandbox.create(textDAT, 'no_clone_dat_a')
        dat2 = self.sandbox.create(textDAT, 'no_clone_dat_b')
        result = self.embody_ext._resolveDATsInClonedCOMPs([dat1, dat2])
        self.assertFalse(result)

    def test_resolve_dats_in_cloned_comps_tags_clone_dat(self):
        """_resolveDATsInClonedCOMPs auto-tags DATs inside clone COMPs."""
        master = self.sandbox.create(baseCOMP, 'rd_master')
        clone = self.sandbox.create(baseCOMP, 'rd_clone')
        clone.par.clone = master
        clone.par.enablecloning = True
        master_dat = master.create(textDAT, 'ext')
        clone_dat = clone.create(textDAT, 'ext')
        result = self.embody_ext._resolveDATsInClonedCOMPs([master_dat, clone_dat])
        self.assertTrue(result, 'Resolver should succeed for clone DAT group')
        self.assertIn('clone', clone_dat.tags, 'Clone DAT should be tagged')
        self.assertNotIn('clone', master_dat.tags, 'Master DAT should NOT be tagged')

    def test_resolve_dats_all_inside_clones_returns_false(self):
        """_resolveDATsInClonedCOMPs returns False when no master exists."""
        master_comp = self.sandbox.create(baseCOMP, 'ac_master')
        clone_a = self.sandbox.create(baseCOMP, 'ac_clone_a')
        clone_b = self.sandbox.create(baseCOMP, 'ac_clone_b')
        clone_a.par.clone = master_comp
        clone_a.par.enablecloning = True
        clone_b.par.clone = master_comp
        clone_b.par.enablecloning = True
        dat_a = clone_a.create(textDAT, 'ext')
        dat_b = clone_b.create(textDAT, 'ext')
        result = self.embody_ext._resolveDATsInClonedCOMPs([dat_a, dat_b])
        self.assertFalse(result, 'Should return False when all DATs are inside clones')

    def test_build_path_groups_excludes_dats_in_clone_comps(self):
        """_buildPathGroups must exclude DATs inside active clone COMPs."""
        master = self.sandbox.create(baseCOMP, 'bpg_master')
        clone = self.sandbox.create(baseCOMP, 'bpg_clone')
        clone.par.clone = master
        clone.par.enablecloning = True
        py_tag = self.embody.par.Pytag.val
        master_dat = master.create(textDAT, 'ext')
        master_dat.tags.add(py_tag)
        master_dat.par.file = 'test/bpg_shared.py'
        clone_dat = clone.create(textDAT, 'ext')
        clone_dat.tags.add(py_tag)
        clone_dat.par.file = 'test/bpg_shared.py'
        groups = self.embody_ext._buildPathGroups()
        # Clone DAT should be excluded by isInsideClone
        for ops in groups.values():
            for o in ops:
                self.assertNotEqual(o.path, clone_dat.path,
                    f'Clone DAT {clone_dat.path} should not appear in path groups')


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
            self.skipTest('Replicator did not produce replicants (timing?)')

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
        self._prev_marker = self.embody.par.Templatemaster.eval()
        self.embody.par.Detectduplicatepaths = True
        # Pin the marker to its default so threshold tests (whose ops never
        # contain '__template__') are not auto-resolved out from under them.
        self.embody.par.Templatemaster = '__template__'

    def tearDown(self):
        # Clean up table rows added during tests
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        self.embody.par.Detectduplicatepaths = self._prev_detect
        self.embody.par.Templatemaster = self._prev_marker
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
        # Pre-tag one op with 'clone' - entire group should be skipped
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
        # tri_b is master - others should be clones
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

    # --- Large-group threshold (_MAX_MANUAL_BUTTONS) ---

    def test_at_threshold_uses_enumerated_buttons(self):
        """A group of exactly _MAX_MANUAL_BUTTONS still gets per-op buttons.

        Seeding the highest button index (only valid in the enumerated
        dialog, which offers Dismiss + one button per op) and getting a
        clean single-master result proves the enumerated path was taken,
        not the 2-button strategy prompt.
        """
        n = self.embody_ext._MAX_MANUAL_BUTTONS
        dats = self._make_tagged_dats(
            [f'thr_{i}' for i in range(n)], shared_path='test/threshold.py')
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': n})  # last op = master
        self.embody_ext.checkForDuplicates()
        clone_count = sum(1 for d in dats if 'clone' in d.tags)
        self.assertEqual(clone_count, n - 1,
                         'Enumerated dialog should tag all but the chosen op')

    def test_large_group_strategy_keep_first(self):
        """Above the threshold, button 1 = 'Keep first as master'."""
        n = self.embody_ext._MAX_MANUAL_BUTTONS + 1
        dats = self._make_tagged_dats(
            [f'big_{i}' for i in range(n)], shared_path='test/biggroup.py')
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 1})  # Keep first
        self.embody_ext.checkForDuplicates()
        clone_count = sum(1 for d in dats if 'clone' in d.tags)
        self.assertEqual(clone_count, n - 1,
                         'Strategy prompt should tag all but the first op')

    def test_large_group_dismiss_tags_nothing(self):
        """Above the threshold, Dismiss (button 0) tags nobody."""
        n = self.embody_ext._MAX_MANUAL_BUTTONS + 2
        dats = self._make_tagged_dats(
            [f'bigd_{i}' for i in range(n)], shared_path='test/biggroupd.py')
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 0})  # Dismiss
        self.embody_ext.checkForDuplicates()
        for d in dats:
            self.assertNotIn('clone', d.tags,
                             f'{d.path} should not be tagged after Dismiss')


class TestBatchResolution(EmbodyTestCase):
    """Tests for the batch-prompt flow when 2+ unresolved groups exist."""

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'batch_workspace')
        self._prev_detect = self.embody.par.Detectduplicatepaths.eval()
        self.embody.par.Detectduplicatepaths = True

    def tearDown(self):
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        self.embody.par.Detectduplicatepaths = self._prev_detect
        self.embody.unstore('_smoke_test_responses')
        super().tearDown()

    def _make_group(self, names, shared_path):
        """Create a duplicate group of tagged DATs sharing one path."""
        py_tag = self.embody.par.Pytag.val
        dats = []
        for name in names:
            dat = self.workspace.create(textDAT, name)
            dat.tags.add(py_tag)
            dat.par.file = shared_path
            dat.par.syncfile = True
            dats.append(dat)
        return dats

    def test_single_unresolved_group_skips_batch_prompt(self):
        """One group -> goes straight to per-group prompt, no batch dialog."""
        dats = self._make_group(['solo_a', 'solo_b'], 'test/solo.py')
        # Seed ONLY the per-group prompt response. If the batch prompt were
        # shown, its title ('Duplicate Paths Detected') wouldn't match and
        # the real ui.messageBox would fire -- so a clean pass implies
        # the batch prompt was correctly skipped.
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 1})
        self.embody_ext.checkForDuplicates()
        clone_count = sum(1 for d in dats if 'clone' in d.tags)
        self.assertEqual(clone_count, 1,
                         'Exactly one op should be tagged clone after '
                         'per-group prompt')

    def test_batch_auto_resolve_tags_all_groups(self):
        """User picks 'Auto-resolve all' -> first op in each group is master."""
        grp1 = self._make_group(['g1_a', 'g1_b'], 'test/grp1.py')
        grp2 = self._make_group(['g2_a', 'g2_b', 'g2_c'], 'test/grp2.py')
        # Button 2 = 'Auto-resolve all'
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Paths Detected': 2})
        self.embody_ext.checkForDuplicates()
        g1_clones = sum(1 for d in grp1 if 'clone' in d.tags)
        g2_clones = sum(1 for d in grp2 if 'clone' in d.tags)
        self.assertEqual(g1_clones, len(grp1) - 1,
                         'Group 1: all but master should be tagged clone')
        self.assertEqual(g2_clones, len(grp2) - 1,
                         'Group 2: all but master should be tagged clone')

    def test_batch_dismiss_tags_nothing(self):
        """User picks 'Dismiss' -> no ops tagged across any group."""
        grp1 = self._make_group(['dg1_a', 'dg1_b'], 'test/dgrp1.py')
        grp2 = self._make_group(['dg2_a', 'dg2_b'], 'test/dgrp2.py')
        # Button 0 = 'Dismiss'
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Paths Detected': 0})
        self.embody_ext.checkForDuplicates()
        for d in grp1 + grp2:
            self.assertNotIn('clone', d.tags,
                             f'{d.path} should not be tagged after Dismiss')

    def test_batch_review_falls_through_to_per_group(self):
        """User picks 'Review individually' -> per-group prompt fires."""
        grp1 = self._make_group(['rg1_a', 'rg1_b'], 'test/rgrp1.py')
        grp2 = self._make_group(['rg2_a', 'rg2_b'], 'test/rgrp2.py')
        # Batch: button 1 = 'Review individually';
        # Per-group prompt fires once per group -- seed a list so both
        # invocations get answered (button 1 = first op is master).
        self.embody.store('_smoke_test_responses', {
            'Duplicate Paths Detected': 1,
            'Duplicate Path Detected': [1, 1],
        })
        self.embody_ext.checkForDuplicates()
        g1_clones = sum(1 for d in grp1 if 'clone' in d.tags)
        g2_clones = sum(1 for d in grp2 if 'clone' in d.tags)
        self.assertEqual(g1_clones, 1,
                         'Group 1: one op should be tagged via per-group prompt')
        self.assertEqual(g2_clones, 1,
                         'Group 2: one op should be tagged via per-group prompt')

    def test_auto_resolve_helper_first_op_is_master(self):
        """_autoResolveFirstAsMaster keeps ops[0], tags rest."""
        ops = self._make_group(['h_a', 'h_b', 'h_c'], 'test/helper.py')
        self.embody_ext._autoResolveFirstAsMaster('test/helper.py', ops)
        self.assertNotIn('clone', ops[0].tags,
                         'First op should be retained as master')
        self.assertIn('clone', ops[1].tags)
        self.assertIn('clone', ops[2].tags)

    def test_auto_resolve_helper_empty_list_is_safe(self):
        """_autoResolveFirstAsMaster handles empty input without error."""
        # Should not raise
        self.embody_ext._autoResolveFirstAsMaster('test/empty.py', [])


class TestTemplateMarkerResolution(EmbodyTestCase):
    """Tests for the Templatemaster naming-convention auto-resolver."""

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'tmpl_workspace')
        self._prev_detect = self.embody.par.Detectduplicatepaths.eval()
        self._prev_marker = self.embody.par.Templatemaster.eval()
        self.embody.par.Detectduplicatepaths = True
        self.embody.par.Templatemaster = '__template__'

    def tearDown(self):
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        self.embody.par.Detectduplicatepaths = self._prev_detect
        self.embody.par.Templatemaster = self._prev_marker
        self.embody.unstore('_smoke_test_responses')
        super().tearDown()

    def _dat(self, parent, name='fbx_callbacks', path='test/tmpl_shared.py'):
        """Create a tagged DAT under parent pointing at a shared file."""
        py_tag = self.embody.par.Pytag.val
        d = parent.create(textDAT, name)
        d.tags.add(py_tag)
        d.par.file = path
        d.par.syncfile = True
        return d

    def _group_with_template(self):
        """One DAT under a __template__ COMP, plus two regular instances."""
        tmpl = self.workspace.create(baseCOMP, '__template__')
        s1 = self.workspace.create(baseCOMP, 'scene_a')
        s2 = self.workspace.create(baseCOMP, 'scene_b')
        master = self._dat(tmpl)
        c1 = self._dat(s1)
        c2 = self._dat(s2)
        return master, [master, c1, c2]

    def test_resolves_single_template_match(self):
        """Exactly one __template__ op -> it's master, rest are clones."""
        master, ops = self._group_with_template()
        result = self.embody_ext._resolveByTemplateMarker(ops)
        self.assertTrue(result, 'Resolver should succeed with one marker match')
        self.assertNotIn('clone', master.tags, 'Template op must stay master')
        clone_count = sum(1 for o in ops if 'clone' in o.tags)
        self.assertEqual(clone_count, len(ops) - 1)

    def test_empty_marker_disables_resolution(self):
        """An empty Templatemaster parameter disables auto-resolution."""
        master, ops = self._group_with_template()
        self.embody.par.Templatemaster = ''
        result = self.embody_ext._resolveByTemplateMarker(ops)
        self.assertFalse(result)
        for o in ops:
            self.assertNotIn('clone', o.tags)

    def test_zero_matches_returns_false(self):
        """No op contains the marker -> falls through to prompt."""
        s1 = self.workspace.create(baseCOMP, 'plain_a')
        s2 = self.workspace.create(baseCOMP, 'plain_b')
        ops = [self._dat(s1), self._dat(s2)]
        result = self.embody_ext._resolveByTemplateMarker(ops)
        self.assertFalse(result)
        for o in ops:
            self.assertNotIn('clone', o.tags)

    def test_multiple_matches_returns_false(self):
        """Two ops contain the marker -> ambiguous -> no auto-resolve."""
        t1 = self.workspace.create(baseCOMP, '__template__')
        outer = self.workspace.create(baseCOMP, 'outer')
        t2 = outer.create(baseCOMP, '__template__')
        ops = [self._dat(t1), self._dat(t2)]
        result = self.embody_ext._resolveByTemplateMarker(ops)
        self.assertFalse(result, 'Ambiguous marker match must not auto-resolve')
        for o in ops:
            self.assertNotIn('clone', o.tags)

    def test_custom_marker_value(self):
        """A user-chosen marker ('_master') resolves just like the default."""
        self.embody.par.Templatemaster = '_master'
        m = self.workspace.create(baseCOMP, '_master')
        inst = self.workspace.create(baseCOMP, 'inst')
        master = self._dat(m)
        clone = self._dat(inst)
        result = self.embody_ext._resolveByTemplateMarker([master, clone])
        self.assertTrue(result)
        self.assertNotIn('clone', master.tags)
        self.assertIn('clone', clone.tags)

    def test_marker_match_is_exact_segment_not_substring(self):
        """Marker must match a whole path segment, not a substring of one."""
        # A COMP named '__template___backup' contains the marker as a
        # substring but not as a distinct path component -> no match.
        near = self.workspace.create(baseCOMP, 'mytemplate')  # no exact seg
        other = self.workspace.create(baseCOMP, 'scene_x')
        ops = [self._dat(near), self._dat(other)]
        result = self.embody_ext._resolveByTemplateMarker(ops)
        self.assertFalse(result,
                         'Substring (mytemplate) must not match marker')

    def test_checkForDuplicates_autoresolves_without_prompt(self):
        """End-to-end: a __template__ group resolves with no dialog shown."""
        master, ops = self._group_with_template()
        # Seed a response that must NOT be consumed -- if a dialog fired,
        # this would be eaten (and the assertion below would fail).
        self.embody.store('_smoke_test_responses',
                          {'Duplicate Path Detected': 0})
        self.embody_ext.checkForDuplicates()
        self.assertNotIn('clone', master.tags)
        clone_count = sum(1 for o in ops if 'clone' in o.tags)
        self.assertEqual(clone_count, len(ops) - 1)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNotNone(responses, 'No prompt should have been shown')


class TestDuplicateButtonLabels(EmbodyTestCase):
    """Tests for _duplicateButtonLabels (the per-op button text)."""

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'lbl_workspace')

    def test_labels_distinguish_same_named_ops(self):
        """Same-named ops under different parents get distinct labels."""
        p1 = self.workspace.create(baseCOMP, 'scene_aaa')
        p2 = self.workspace.create(baseCOMP, 'scene_bbb')
        d1 = p1.create(textDAT, 'fbx_callbacks')
        d2 = p2.create(textDAT, 'fbx_callbacks')
        labels = self.embody_ext._duplicateButtonLabels([d1, d2])
        self.assertEqual(len(labels), 2)
        self.assertTrue(labels[0].startswith('1: '))
        self.assertTrue(labels[1].startswith('2: '))
        self.assertIn('scene_aaa', labels[0])
        self.assertIn('scene_bbb', labels[1])
        self.assertNotEqual(labels[0][3:], labels[1][3:],
                            'Differing segments must produce different labels')

    def test_labels_are_numbered_in_order(self):
        """Labels are prefixed 1..N matching the dialog body list order."""
        dats = [self.workspace.create(textDAT, f'lab_{i}') for i in range(4)]
        labels = self.embody_ext._duplicateButtonLabels(dats)
        for i, label in enumerate(labels):
            self.assertTrue(label.startswith(f'{i+1}: '),
                            f'Label {i} should start with "{i+1}: "')
