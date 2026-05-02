"""
Test suite: Ancestor rename detection and batch handling.

Tests the ancestor rename system that detects when a parent COMP is renamed
and batch-updates all externalized children in a single operation:
  - _detectAncestorRename: threshold, prefix extraction, verification
  - _handleAncestorRename: disk segment computation, directory rename,
    table updates, parameter updates, return value semantics
  - checkOpsForContinuity fallback: per-operator handling on batch failure
  - ExternalizationsFolder prefix: the core bug from issue #16
"""

from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestAncestorRename(EmbodyTestCase):

    def setUp(self):
        """Create workspace, temp dirs, and intercept messageBox."""
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')
        self._test_dir = Path(project.folder) / 'embody' / 'unit_tests' / '_test_ancestor'
        self._test_dir.mkdir(parents=True, exist_ok=True)
        self._added_paths = []

        # Snapshot top-level dirs under dev/embody/ so tearDown can remove
        # any test-created prefix dirs (retval/, tblupd/, cancel_test/, etc.)
        # AND their renamed siblings (the rename happens inside the same prefix,
        # so removing the prefix kills both source and renamed-to dirs).
        # Without this, a successful rename leaves the renamed-to dir on disk;
        # the next run hits the (correct) "Target directory already exists"
        # guard in _handleAncestorRename and fails.
        embody_root = Path(project.folder) / 'embody'
        self._embody_snapshot = (
            {p.name for p in embody_root.iterdir() if p.is_dir()}
            if embody_root.exists() else set()
        )

        # Intercept _messageBox so tests never block on UI
        self._captured_dialogs = []
        self._orig_messageBox = self.embody_ext._messageBox
        self._scripted_choice = 1  # Default: "Proceed"

        def _stub(title, message, buttons):
            self._captured_dialogs.append({
                'title': title, 'message': message, 'buttons': buttons
            })
            return self._scripted_choice

        self.embody_ext._messageBox = _stub

    def tearDown(self):
        self.embody_ext._messageBox = self._orig_messageBox
        # Clean up table entries
        table = self.embody_ext.Externalizations
        for path in self._added_paths:
            for i in range(table.numRows - 1, 0, -1):
                if table[i, 'path'].val == path:
                    table.deleteRow(i)
        # Also clean sandbox-generated entries
        for i in range(table.numRows - 1, 0, -1):
            path = table[i, 'path'].val
            if path.startswith(self.sandbox.path):
                table.deleteRow(i)
        # Clean up filesystem
        import shutil
        # 1. Any new top-level dirs under dev/embody/ that the test created
        #    (retval/, tblupd/, cancel_test/, conflict/, phaseA/, tdntest/, ...)
        #    Includes renamed-to subdirs since they live inside the same prefix.
        embody_root = Path(project.folder) / 'embody'
        if embody_root.exists():
            for p in embody_root.iterdir():
                if p.is_dir() and p.name not in self._embody_snapshot:
                    shutil.rmtree(p, ignore_errors=True)
        # 2. The workspace dir under the sandbox -- the no-ext-folder test
        #    writes bare_parent/ and renames to bare_renamed/ here.
        workspace_disk = Path(project.folder) / self.workspace.path.lstrip('/')
        if workspace_disk.exists():
            shutil.rmtree(workspace_disk, ignore_errors=True)
        # 3. Legacy _test_dir (kept for backwards compat)
        if self._test_dir.exists():
            shutil.rmtree(self._test_dir, ignore_errors=True)
        super().tearDown()

    # --- Helpers ---

    def _add_table_entry(self, path, op_type, strategy, rel_file_path):
        """Add a row to the externalizations table directly."""
        table = self.embody_ext.Externalizations
        table.appendRow([
            path, op_type, strategy, rel_file_path,
            '2026-01-01 00:00:00 UTC', '', '', ''
        ])
        self._added_paths.append(path)

    def _create_dir(self, rel_path):
        """Create a directory under project.folder."""
        d = Path(project.folder) / rel_path
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _create_file(self, rel_path, content='# test'):
        """Create a file on disk at the given relative path under project.folder."""
        abs_path = Path(project.folder) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding='utf-8')
        return abs_path

    def _table_has_path(self, path):
        """Check if the externalizations table has an entry for the given path."""
        table = self.embody_ext.Externalizations
        for i in range(1, table.numRows):
            if table[i, 'path'].val == path:
                return True
        return False

    def _get_table_rel_file(self, path):
        """Get the rel_file_path for a given path in the externalizations table."""
        table = self.embody_ext.Externalizations
        for i in range(1, table.numRows):
            if table[i, 'path'].val == path:
                return table[i, 'rel_file_path'].val
        return None

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

    def _build_rows_to_check(self, paths_and_files):
        """Build rows_to_check list from (path, rel_file, strategy) tuples."""
        return [(p, rf, 'baseCOMP', s) for p, rf, s in paths_and_files]

    # =========================================================================
    # _detectAncestorRename — threshold behavior
    # =========================================================================

    def test_detect_returns_none_with_two_missing(self):
        """Two missing operators is below the 3-op threshold — returns None."""
        parent_comp = self.workspace.create(baseCOMP, 'parent')
        c1, _, _ = self._externalize_comp(parent_comp, 'child1')
        c2, _, _ = self._externalize_comp(parent_comp, 'child2')

        # Build rows_to_check pointing to old paths
        rows = []
        for i in range(1, self.embody_ext.Externalizations.numRows):
            p = self.embody_ext.Externalizations[i, 'path'].val
            if p.startswith(parent_comp.path + '/'):
                rf = self.embody_ext.Externalizations[i, 'rel_file_path'].val
                rows.append((p, rf, 'baseCOMP', ''))

        # Rename parent so children go "missing"
        parent_comp.name = 'parent_renamed'

        result = self.embody_ext._detectAncestorRename(rows)
        self.assertIsNone(result, 'Should return None with only 2 missing ops')

    def test_detect_returns_tuple_with_three_missing(self):
        """Three missing operators with common prefix should be detected."""
        parent_comp = self.workspace.create(baseCOMP, 'detect3')
        c1, _, _ = self._externalize_comp(parent_comp, 'a1')
        c2, _, _ = self._externalize_comp(parent_comp, 'a2')
        c3, _, _ = self._externalize_comp(parent_comp, 'a3')

        rows = []
        for i in range(1, self.embody_ext.Externalizations.numRows):
            p = self.embody_ext.Externalizations[i, 'path'].val
            if p.startswith(parent_comp.path + '/'):
                rf = self.embody_ext.Externalizations[i, 'rel_file_path'].val
                rows.append((p, rf, 'baseCOMP', ''))

        old_parent_path = parent_comp.path
        parent_comp.name = 'detect3_renamed'

        result = self.embody_ext._detectAncestorRename(rows)
        self.assertIsNotNone(result, 'Should detect ancestor rename with 3+ missing ops')
        old_prefix, new_prefix = result
        self.assertIn('detect3', old_prefix)
        self.assertIn('detect3_renamed', new_prefix)

    def test_detect_returns_none_when_ancestor_still_exists(self):
        """If the ancestor COMP still exists at the old path, returns None."""
        parent_comp = self.workspace.create(baseCOMP, 'still_here')
        # Inject fake missing entries pointing under this parent
        base = parent_comp.path
        rows = [
            (base + '/fake1', 'embody' + base + '/fake1/fake1.tox', 'baseCOMP', ''),
            (base + '/fake2', 'embody' + base + '/fake2/fake2.tox', 'baseCOMP', ''),
            (base + '/fake3', 'embody' + base + '/fake3/fake3.tox', 'baseCOMP', ''),
        ]
        # Parent still exists at old path — detection should fail
        result = self.embody_ext._detectAncestorRename(rows)
        self.assertIsNone(result, 'Should return None when ancestor still exists')

    def test_detect_returns_none_with_one_missing(self):
        """One missing operator should not trigger ancestor detection."""
        parent_comp = self.workspace.create(baseCOMP, 'one_missing')
        c1, _, _ = self._externalize_comp(parent_comp, 'only_child')

        rows = []
        for i in range(1, self.embody_ext.Externalizations.numRows):
            p = self.embody_ext.Externalizations[i, 'path'].val
            if p.startswith(parent_comp.path + '/'):
                rf = self.embody_ext.Externalizations[i, 'rel_file_path'].val
                rows.append((p, rf, 'baseCOMP', ''))

        parent_comp.name = 'one_missing_renamed'

        result = self.embody_ext._detectAncestorRename(rows)
        self.assertIsNone(result, 'Should return None with only 1 missing op')

    # =========================================================================
    # _handleAncestorRename — disk segment computation (issue #16 core fix)
    # =========================================================================

    def test_disk_segment_includes_ext_folder(self):
        """With ExternalizationsFolder set, rel_file matching must include the prefix."""
        ext_folder = self.embody_ext.ExternalizationsFolder
        if not ext_folder:
            return  # Skip if no folder configured

        old_prefix = '/test_project/scene'
        new_prefix = '/test_project/myscene'
        old_seg = 'test_project/scene'
        new_seg = 'test_project/myscene'

        old_rel = ext_folder + '/' + old_seg + '/comp1/comp1.tox'
        new_rel_expected = ext_folder + '/' + new_seg + '/comp1/comp1.tox'

        # Create the source directory so Phase C can succeed
        self._create_dir(ext_folder + '/' + old_seg + '/comp1')
        self._create_file(ext_folder + '/' + old_seg + '/comp1/comp1.tox', 'fake')

        # Create operators at the NEW paths so Phase E can find them
        test_parent = self.workspace.create(baseCOMP, 'test_project')
        scene = test_parent.create(baseCOMP, 'myscene')
        comp1 = scene.create(baseCOMP, 'comp1')
        tox_tag = self.embody.par.Toxtag.val
        comp1.tags.add(tox_tag)
        comp1.par.externaltox = old_rel
        comp1.par.externaltox.readOnly = True

        # Add table entry with OLD path
        self._add_table_entry(
            old_prefix + '/comp1', 'baseCOMP', '', old_rel)

        rows = [(old_prefix + '/comp1', old_rel, 'baseCOMP', '')]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, new_prefix, rows, ext_folder)

        self.assertTrue(result, 'Should succeed with ExternalizationsFolder prefix')

        # Verify directory was renamed
        old_dir = Path(project.folder) / ext_folder / old_seg
        new_dir = Path(project.folder) / ext_folder / new_seg
        self.assertFalse(old_dir.exists(), 'Old directory should be gone')
        self.assertTrue(new_dir.exists(), 'New directory should exist')

    def test_disk_segment_works_without_ext_folder(self):
        """With empty ExternalizationsFolder, disk segments equal bare OP path segments."""
        old_prefix = self.workspace.path + '/bare_parent'
        new_prefix = self.workspace.path + '/bare_renamed'
        old_seg = old_prefix.strip('/')
        new_seg = new_prefix.strip('/')

        # Create directory matching bare OP path (no ext folder prefix)
        self._create_dir(old_seg + '/child1')
        self._create_file(old_seg + '/child1/child1.tox', 'fake')

        # Create the operator at new path
        bare_parent = self.workspace.create(baseCOMP, 'bare_renamed')
        child1 = bare_parent.create(baseCOMP, 'child1')
        tox_tag = self.embody.par.Toxtag.val
        child1.tags.add(tox_tag)
        child1.par.externaltox = old_seg + '/child1/child1.tox'
        child1.par.externaltox.readOnly = True

        self._add_table_entry(
            old_prefix + '/child1', 'baseCOMP', '',
            old_seg + '/child1/child1.tox')

        rows = [(old_prefix + '/child1',
                 old_seg + '/child1/child1.tox', 'baseCOMP', '')]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, new_prefix, rows, '')  # Empty ext folder

        self.assertTrue(result, 'Should succeed with empty ExternalizationsFolder')

    # =========================================================================
    # _handleAncestorRename — Phase A rel_file matching
    # =========================================================================

    def test_phase_a_matches_rel_files_with_folder_prefix(self):
        """Phase A must match rel_file paths that include ExternalizationsFolder."""
        ext_folder = self.embody_ext.ExternalizationsFolder
        if not ext_folder:
            return

        old_prefix = '/phaseA/group'
        new_prefix = '/phaseA/newgroup'
        old_seg = 'phaseA/group'

        old_rel = ext_folder + '/' + old_seg + '/op1/op1.tox'

        # Create source dir and operator
        self._create_dir(ext_folder + '/' + old_seg + '/op1')
        self._create_file(ext_folder + '/' + old_seg + '/op1/op1.tox', 'fake')

        pa = self.workspace.create(baseCOMP, 'phaseA')
        grp = pa.create(baseCOMP, 'newgroup')
        op1 = grp.create(baseCOMP, 'op1')
        op1.par.externaltox = old_rel
        op1.par.externaltox.readOnly = True

        self._add_table_entry(old_prefix + '/op1', 'baseCOMP', '', old_rel)

        rows = [(old_prefix + '/op1', old_rel, 'baseCOMP', '')]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, new_prefix, rows, ext_folder)

        self.assertTrue(result, 'Phase A should match rel_file with folder prefix')

        # Verify table was updated with new rel_file
        new_rel = self._get_table_rel_file(new_prefix + '/op1')
        self.assertIsNotNone(new_rel, 'Table should have new path entry')
        self.assertIn('newgroup', new_rel, 'New rel_file should contain renamed segment')

    def test_phase_a_empty_affected_returns_false(self):
        """If no rows match the prefix, Phase A produces empty affected list → False."""
        # Rows with paths that don't match old_prefix
        rows = [('/unrelated/op1', 'embody/unrelated/op1.tox', 'baseCOMP', '')]

        result = self.embody_ext._handleAncestorRename(
            '/totally/different', '/totally/renamed', rows, 'embody')

        self.assertFalse(result, 'Should return False when no rows match prefix')

    # =========================================================================
    # _handleAncestorRename — user cancellation
    # =========================================================================

    def test_user_cancel_returns_false(self):
        """User clicking Cancel should return False without renaming."""
        ext_folder = self.embody_ext.ExternalizationsFolder or 'embody'
        old_prefix = '/cancel_test/parent'

        old_rel = ext_folder + '/cancel_test/parent/c1/c1.tox'
        self._create_dir(ext_folder + '/cancel_test/parent/c1')
        self._create_file(ext_folder + '/cancel_test/parent/c1/c1.tox', 'fake')

        self._add_table_entry(old_prefix + '/c1', 'baseCOMP', '', old_rel)

        rows = [(old_prefix + '/c1', old_rel, 'baseCOMP', '')]

        self._scripted_choice = 0  # Cancel
        result = self.embody_ext._handleAncestorRename(
            old_prefix, '/cancel_test/renamed', rows, ext_folder)

        self.assertFalse(result, 'User cancel should return False')
        # Directory should still exist at old location
        old_dir = Path(project.folder) / ext_folder / 'cancel_test/parent'
        self.assertTrue(old_dir.exists(), 'Directory should not be renamed on cancel')

    # =========================================================================
    # _handleAncestorRename — Phase C error cases
    # =========================================================================

    def test_source_dir_not_found_returns_false(self):
        """Missing source directory should return False (the original bug)."""
        ext_folder = self.embody_ext.ExternalizationsFolder or 'embody'
        old_prefix = '/nosource/parent'

        old_rel = ext_folder + '/nosource/parent/c1/c1.tox'
        # Deliberately do NOT create the source directory
        self._add_table_entry(old_prefix + '/c1', 'baseCOMP', '', old_rel)

        rows = [(old_prefix + '/c1', old_rel, 'baseCOMP', '')]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, '/nosource/renamed', rows, ext_folder)

        self.assertFalse(result, 'Should return False when source dir missing')
        # Verify error dialog was shown
        self.assertGreater(len(self._captured_dialogs), 0,
                           'Should show error dialog')
        self.assertIn('Source directory not found',
                      self._captured_dialogs[-1]['message'])

    def test_target_dir_exists_returns_false(self):
        """Existing target directory should return False."""
        ext_folder = self.embody_ext.ExternalizationsFolder or 'embody'
        old_prefix = '/conflict/parent'

        old_rel = ext_folder + '/conflict/parent/c1/c1.tox'
        # Create BOTH source and target directories
        self._create_dir(ext_folder + '/conflict/parent/c1')
        self._create_file(ext_folder + '/conflict/parent/c1/c1.tox', 'fake')
        self._create_dir(ext_folder + '/conflict/renamed')

        self._add_table_entry(old_prefix + '/c1', 'baseCOMP', '', old_rel)

        rows = [(old_prefix + '/c1', old_rel, 'baseCOMP', '')]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, '/conflict/renamed', rows, ext_folder)

        self.assertFalse(result, 'Should return False when target dir exists')

    # =========================================================================
    # _handleAncestorRename — Phase D table updates
    # =========================================================================

    def test_table_entries_updated_after_rename(self):
        """Table path and rel_file_path columns should reflect the new name."""
        ext_folder = self.embody_ext.ExternalizationsFolder or 'embody'
        old_prefix = '/tblupd/scene'
        new_prefix = '/tblupd/newscene'

        old_rel1 = ext_folder + '/tblupd/scene/comp1/comp1.tox'
        old_rel2 = ext_folder + '/tblupd/scene/comp2/comp2.tox'

        # Create dirs and files
        self._create_dir(ext_folder + '/tblupd/scene/comp1')
        self._create_file(ext_folder + '/tblupd/scene/comp1/comp1.tox', 'fake')
        self._create_dir(ext_folder + '/tblupd/scene/comp2')
        self._create_file(ext_folder + '/tblupd/scene/comp2/comp2.tox', 'fake')

        # Create operators at new paths
        tblupd = self.workspace.create(baseCOMP, 'tblupd')
        newscene = tblupd.create(baseCOMP, 'newscene')
        c1 = newscene.create(baseCOMP, 'comp1')
        c2 = newscene.create(baseCOMP, 'comp2')
        c1.par.externaltox = old_rel1
        c1.par.externaltox.readOnly = True
        c2.par.externaltox = old_rel2
        c2.par.externaltox.readOnly = True

        self._add_table_entry(old_prefix + '/comp1', 'baseCOMP', '', old_rel1)
        self._add_table_entry(old_prefix + '/comp2', 'baseCOMP', '', old_rel2)

        rows = [
            (old_prefix + '/comp1', old_rel1, 'baseCOMP', ''),
            (old_prefix + '/comp2', old_rel2, 'baseCOMP', ''),
        ]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, new_prefix, rows, ext_folder)

        self.assertTrue(result, 'Rename should succeed')

        # Verify both table entries updated
        self.assertTrue(self._table_has_path(new_prefix + '/comp1'),
                        'Table should have new path for comp1')
        self.assertTrue(self._table_has_path(new_prefix + '/comp2'),
                        'Table should have new path for comp2')

        # Verify rel_file_paths updated
        new_rel1 = self._get_table_rel_file(new_prefix + '/comp1')
        self.assertIn('newscene', new_rel1, 'rel_file should contain new name')
        self.assertNotIn('/scene/', new_rel1,
                         'rel_file should not contain old segment')

    # =========================================================================
    # _handleAncestorRename — TDN strategy
    # =========================================================================

    def test_tdn_strategy_skips_param_update_but_updates_table(self):
        """TDN-strategy ops should have table updated but skip externaltox changes."""
        ext_folder = self.embody_ext.ExternalizationsFolder or 'embody'
        old_prefix = '/tdntest/parent'
        new_prefix = '/tdntest/newparent'

        old_rel = ext_folder + '/tdntest/parent/tdn_comp.tdn'

        self._create_dir(ext_folder + '/tdntest/parent')
        self._create_file(ext_folder + '/tdntest/parent/tdn_comp.tdn', '{}')

        # Create op at new path
        tdntest = self.workspace.create(baseCOMP, 'tdntest')
        newparent = tdntest.create(baseCOMP, 'newparent')
        tdn_comp = newparent.create(baseCOMP, 'tdn_comp')

        self._add_table_entry(old_prefix + '/tdn_comp', 'baseCOMP', 'tdn', old_rel)

        rows = [(old_prefix + '/tdn_comp', old_rel, 'baseCOMP', 'tdn')]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, new_prefix, rows, ext_folder)

        self.assertTrue(result, 'Should succeed for TDN ops')
        self.assertTrue(self._table_has_path(new_prefix + '/tdn_comp'),
                        'Table should have new path for TDN op')

    # =========================================================================
    # _handleAncestorRename — return value semantics
    # =========================================================================

    def test_returns_true_on_success(self):
        """Successful rename should return True."""
        ext_folder = self.embody_ext.ExternalizationsFolder or 'embody'
        old_prefix = '/retval/parent'
        new_prefix = '/retval/renamed'

        old_rel = ext_folder + '/retval/parent/c1/c1.tox'
        self._create_dir(ext_folder + '/retval/parent/c1')
        self._create_file(ext_folder + '/retval/parent/c1/c1.tox', 'fake')

        retval = self.workspace.create(baseCOMP, 'retval')
        renamed = retval.create(baseCOMP, 'renamed')
        c1 = renamed.create(baseCOMP, 'c1')
        c1.par.externaltox = old_rel
        c1.par.externaltox.readOnly = True

        self._add_table_entry(old_prefix + '/c1', 'baseCOMP', '', old_rel)

        rows = [(old_prefix + '/c1', old_rel, 'baseCOMP', '')]

        result = self.embody_ext._handleAncestorRename(
            old_prefix, new_prefix, rows, ext_folder)

        self.assertTrue(result, '_handleAncestorRename should return True on success')

    def test_returns_false_on_failure(self):
        """Failed rename (missing source dir) should return False."""
        result = self.embody_ext._handleAncestorRename(
            '/nonexistent/old', '/nonexistent/new',
            [('/nonexistent/old/c1', 'embody/nonexistent/old/c1/c1.tox', 'baseCOMP', '')],
            'embody')

        self.assertFalse(result, '_handleAncestorRename should return False on failure')

    # =========================================================================
    # checkOpsForContinuity — fallback on batch failure
    # =========================================================================

    def test_continuity_falls_back_on_ancestor_failure(self):
        """When ancestor rename fails, per-operator handling should still run."""
        # Create 3+ externalized COMPs under a parent, then rename the parent.
        # Deliberately break the ancestor rename (don't create dirs) so it fails.
        # The per-operator fallback should then handle each one individually.
        parent = self.workspace.create(baseCOMP, 'fallback_parent')
        c1, p1, r1 = self._externalize_comp(parent, 'fb1')
        c2, p2, r2 = self._externalize_comp(parent, 'fb2')
        c3, p3, r3 = self._externalize_comp(parent, 'fb3')

        # Delete the source directory to ensure ancestor rename fails
        ext_folder = self.embody_ext.ExternalizationsFolder
        old_dir_seg = parent.path.strip('/')
        if ext_folder:
            disk_dir = Path(project.folder) / ext_folder / old_dir_seg
        else:
            disk_dir = Path(project.folder) / old_dir_seg

        # Rename the parent COMP
        parent.name = 'fallback_renamed'
        new_parent_path = parent.path

        # Remove the disk directory so _handleAncestorRename fails
        import shutil
        if disk_dir.exists():
            shutil.rmtree(disk_dir)

        # Run full continuity check — should detect ancestor rename,
        # fail on it, then fall back to per-operator handling.
        self.embody_ext.checkOpsForContinuity(ext_folder)

        # After fallback, individual rename detection (_findMovedOp) should
        # have picked up the renamed operators via externaltox matching.
        # Check that at least one of the new paths is now in the table.
        found_any_new = False
        for i in range(1, self.embody_ext.Externalizations.numRows):
            p = self.embody_ext.Externalizations[i, 'path'].val
            if p.startswith(new_parent_path + '/'):
                found_any_new = True
                break

        self.assertTrue(found_any_new,
                        'Fallback per-operator handling should update at least one entry')

    # =========================================================================
    # Integration: full ancestor rename flow with real externalized ops
    # =========================================================================

    def test_full_ancestor_rename_with_externalized_ops(self):
        """End-to-end: rename parent COMP → continuity check updates everything."""
        parent = self.workspace.create(baseCOMP, 'full_parent')
        c1, old_p1, old_r1 = self._externalize_comp(parent, 'full_c1')
        c2, old_p2, old_r2 = self._externalize_comp(parent, 'full_c2')
        c3, old_p3, old_r3 = self._externalize_comp(parent, 'full_c3')

        # Verify old paths in table
        self.assertTrue(self._table_has_path(old_p1))
        self.assertTrue(self._table_has_path(old_p2))
        self.assertTrue(self._table_has_path(old_p3))

        # Rename parent
        parent.name = 'full_renamed'
        new_parent_path = parent.path

        # Run continuity check
        ext_folder = self.embody_ext.ExternalizationsFolder
        self.embody_ext.checkOpsForContinuity(ext_folder)

        # Old paths should be gone
        self.assertFalse(self._table_has_path(old_p1),
                         'Old path should be removed from table')

        # New paths should be present
        new_p1 = new_parent_path + '/full_c1'
        new_p2 = new_parent_path + '/full_c2'
        new_p3 = new_parent_path + '/full_c3'
        self.assertTrue(self._table_has_path(new_p1),
                        'New path should be in table for full_c1')
        self.assertTrue(self._table_has_path(new_p2),
                        'New path should be in table for full_c2')
        self.assertTrue(self._table_has_path(new_p3),
                        'New path should be in table for full_c3')

        # rel_file_path should contain the new parent name
        new_rel = self._get_table_rel_file(new_p1)
        self.assertIn('full_renamed', new_rel,
                      'rel_file should contain renamed parent name')

    def test_full_ancestor_rename_moves_directory(self):
        """Ancestor rename should move the disk directory atomically."""
        parent = self.workspace.create(baseCOMP, 'dir_parent')
        c1, _, r1 = self._externalize_comp(parent, 'dir_c1')
        c2, _, r2 = self._externalize_comp(parent, 'dir_c2')
        c3, _, r3 = self._externalize_comp(parent, 'dir_c3')

        ext_folder = self.embody_ext.ExternalizationsFolder
        old_seg = parent.path.strip('/')
        if ext_folder:
            old_disk = Path(project.folder) / ext_folder / old_seg
        else:
            old_disk = Path(project.folder) / old_seg
        self.assertTrue(old_disk.exists(), 'Old directory should exist before rename')

        parent.name = 'dir_renamed'

        self.embody_ext.checkOpsForContinuity(ext_folder)

        new_seg = parent.path.strip('/')
        if ext_folder:
            new_disk = Path(project.folder) / ext_folder / new_seg
        else:
            new_disk = Path(project.folder) / new_seg

        self.assertFalse(old_disk.exists(), 'Old directory should be gone')
        self.assertTrue(new_disk.exists(), 'New directory should exist')

    # =========================================================================
    # _messageBox interception verification
    # =========================================================================

    def test_confirmation_dialog_shown(self):
        """The Ancestor Rename Detected dialog should be shown during rename."""
        ext_folder = self.embody_ext.ExternalizationsFolder or 'embody'
        old_prefix = '/dlgtest/parent'
        new_prefix = '/dlgtest/renamed'

        old_rel = ext_folder + '/dlgtest/parent/c1/c1.tox'
        self._create_dir(ext_folder + '/dlgtest/parent/c1')
        self._create_file(ext_folder + '/dlgtest/parent/c1/c1.tox', 'fake')

        dlg = self.workspace.create(baseCOMP, 'dlgtest')
        renamed = dlg.create(baseCOMP, 'renamed')
        c1 = renamed.create(baseCOMP, 'c1')
        c1.par.externaltox = old_rel
        c1.par.externaltox.readOnly = True

        self._add_table_entry(old_prefix + '/c1', 'baseCOMP', '', old_rel)

        rows = [(old_prefix + '/c1', old_rel, 'baseCOMP', '')]

        self.embody_ext._handleAncestorRename(
            old_prefix, new_prefix, rows, ext_folder)

        self.assertGreater(len(self._captured_dialogs), 0,
                           'At least one dialog should be shown')
        self.assertEqual(self._captured_dialogs[0]['title'],
                         'Embody -- Ancestor Rename Detected')
